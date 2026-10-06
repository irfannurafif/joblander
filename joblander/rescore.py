"""Reassess pending leads in place, using the existing sourcing scorer."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from joblander import sourcing
from joblander.applyops import list_pending, _proposal_file, _ws_file


def pending_leads(cfg) -> list[Path]:
    return [Path(p['_file']) for p in list_pending(cfg)
            if p.get('kind') == 'lead.intake']


def resolve_lead(cfg, lead_id: str) -> Path:
    candidates = pending_leads(cfg)
    if '/' in lead_id or '\\' in lead_id:
        path = _proposal_file(cfg, lead_id)
        matches = [p for p in candidates if p.resolve() == path]
    else:
        matches = [p for p in candidates if lead_id in (p.name, p.stem)]
    if len(matches) != 1:
        raise ValueError(f'Expected one pending New Lead for {lead_id!r}; found {len(matches)}')
    return _proposal_file(cfg, matches[0])


def _saved_jd(cfg, proposal):
    if proposal.get('raw_text'):
        return proposal['raw_text'], 'saved original text'
    attachment = _ws_file(cfg, proposal.get('jd_file') or '')
    if attachment:
        from joblander.company import _file_text
        text = _file_text(attachment)
        if text.strip() and not text.startswith('（PDF 抽取失败：'):
            return text, 'saved attachment'
    text = proposal['lead'].get('jd_excerpt') or proposal.get('raw_excerpt') or ''
    return text, 'saved excerpt (limited context)' if text else 'no saved JD (limited context)'


def rescore_lead(cfg, llm, lead_id: str) -> dict:
    path = resolve_lead(cfg, lead_id)
    original_stat = path.stat()
    original = path.read_bytes()
    proposal = json.loads(original)
    if not sourcing.load_prefs(cfg):
        raise ValueError('Configure Search Preferences before rescoring')
    jd, context = _saved_jd(cfg, proposal)
    fit = sourcing.assess_fit(cfg, llm, proposal['lead'], jd_text=jd)
    if not fit or type(fit.get('fit')) is not int or not 1 <= fit['fit'] <= 5:
        raise ValueError('Scorer returned no usable score; previous assessment preserved')
    proposal['fit'] = fit
    # Stage the replacement before checking for approval, retirement or edits.
    fd, tmp = tempfile.mkstemp(prefix='.rescore-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(proposal, stream, ensure_ascii=False, indent=1)
        # New Leads derives its intake date from mtime; keep that date stable.
        os.chmod(tmp, original_stat.st_mode & 0o777)
        os.utime(tmp, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        if resolve_lead(cfg, str(path)) != path or path.read_bytes() != original:
            raise ValueError('Lead changed during scoring; previous assessment preserved')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {'lead_id': path.name, 'fit': fit['fit'], 'context': context}


def rescore_all_new(cfg, llm, *, progress: Callable[[dict], None] | None = None) -> dict:
    """Rescore a fixed batch; optionally report start/success/failure events."""
    paths = pending_leads(cfg)
    summary = {'rescored': len(paths), 'changed': 0, 'unchanged': 0, 'failed': 0}
    result = {'updated': [], 'failed': [], 'summary': summary}
    for index, path in enumerate(paths, 1):
        event = {'index': index, 'total': len(paths), 'lead_id': path.name,
                 'company': 'Unknown company', 'position': 'Unknown position',
                 'old_score': None}
        preparation_error = None
        try:
            proposal = json.loads(_proposal_file(cfg, path).read_bytes())
            lead = proposal.get('lead') or {}
            event.update(company=lead.get('company') or event['company'],
                         position=lead.get('position') or event['position'],
                         old_score=(proposal.get('fit') or {}).get('fit'))
        except Exception as exc:
            preparation_error = exc
        if progress:
            progress({**event, 'status': 'start'})
        try:
            if preparation_error is not None:
                raise preparation_error
            updated = rescore_lead(cfg, llm, str(path))
        except Exception as exc:
            result['failed'].append({'lead_id': path.name, 'error': str(exc)})
            summary['failed'] += 1
            if progress:
                progress({**event, 'status': 'failed', 'error': str(exc)})
        else:
            result['updated'].append(updated)
            summary['unchanged' if event['old_score'] == updated['fit'] else 'changed'] += 1
            if progress:
                progress({**event, 'status': 'success', 'new_score': updated['fit'],
                          'context': updated['context']})
    return result
