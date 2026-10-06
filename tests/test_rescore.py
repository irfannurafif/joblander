import json

import pytest

from joblander.config import Config
from joblander import rescore, sourcing
from joblander.__main__ import main


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cfg = Config(raw={'workspace_dir': str(tmp_path)}, path=tmp_path / 'c.yaml')
    (tmp_path / '12-intake').mkdir()
    (tmp_path / '03-materials').mkdir()
    sourcing.save_prefs(cfg, {'intent': 'current preferences'})
    (tmp_path / '03-materials/achievement-bank.md').write_text('current Arsenal')
    monkeypatch.setattr('joblander.config.load_config', lambda: cfg)
    return cfg


def lead(cfg, name='one', **extra):
    path = cfg.workspace_dir / '12-intake' / f'{name}.json'
    data = {'kind': 'lead.intake', 'approved': None,
            'lead': {'company': 'Example', 'position': 'Engineer', 'jd_excerpt': 'saved JD'},
            'fit': {'fit': 2, 'req_gaps': [{'req': 'old gap'}]},
            'metadata': {'custom': 'preserve'}, **extra}
    path.write_text(json.dumps(data))
    return path


class Model:
    def generate(self, prompt, **kwargs):
        assert 'current preferences' in prompt and 'current Arsenal' in prompt
        assert kwargs['system'] == sourcing.FIT_SYSTEM
        assert 'saved JD' in prompt
        return json.dumps({'fit': 4, 'why': 'new reason', 'flags': ['new flag'],
                           'req_gaps': [{'req': 'new gap', 'verdict': '存疑'}]})


def test_single_preserves_all_except_fit_and_repeats(cfg):
    path = lead(cfg)
    original = json.loads(path.read_text())
    intake_time = path.stat().st_mtime_ns
    for identifier in [path.stem, path.name, str(path)]:
        result = rescore.rescore_lead(cfg, Model(), identifier)
        assert result['fit'] == 4 and 'limited context' in result['context']
    updated = json.loads(path.read_text())
    assert updated['fit']['req_gaps'] == [{'req': 'new gap', 'verdict': '存疑'}]
    assert {k: v for k, v in updated.items() if k != 'fit'} == {
        k: v for k, v in original.items() if k != 'fit'}
    assert list(path.parent.glob('*.json')) == [path]
    assert path.stat().st_mtime_ns == intake_time


def test_current_inputs_reloaded(cfg):
    path = lead(cfg)
    rescore.rescore_lead(cfg, Model(), path.stem)
    sourcing.save_prefs(cfg, {'intent': 'changed preferences'})
    (cfg.workspace_dir / '03-materials/achievement-bank.md').write_text('changed Arsenal')
    class Changed:
        def generate(self, prompt, **kw):
            assert 'changed preferences' in prompt and 'changed Arsenal' in prompt
            return '{"fit": 3}'
    assert rescore.rescore_lead(cfg, Changed(), path.stem)['fit'] == 3


@pytest.mark.parametrize('response', ['{}', 'bad json', '{"fit": 9}', '{"fit": null}'])
def test_invalid_assessment_preserves_bytes(cfg, response):
    path = lead(cfg)
    before = path.read_bytes()
    class Bad:
        def generate(self, *a, **kw):
            return response
    with pytest.raises(ValueError):
        rescore.rescore_lead(cfg, Bad(), path.stem)
    assert path.read_bytes() == before


def test_missing_preferences(cfg):
    path = lead(cfg)
    (cfg.workspace_dir / sourcing.PREFS_REL).unlink()
    before = path.read_bytes()
    with pytest.raises(ValueError, match='Preferences'):
        rescore.rescore_lead(cfg, None, path.stem)
    assert path.read_bytes() == before


def test_batch_isolated_failures_and_pending_only(cfg):
    good = lead(cfg, 'good')
    bad = lead(cfg, 'bad', lead={'company': 'bad'})
    done = lead(cfg, 'done', approved=True)
    other = lead(cfg, 'other', kind='company.update')
    before = {p: p.read_bytes() for p in [bad, done, other]}
    result = rescore.rescore_all_new(cfg, Model())
    assert [r['lead_id'] for r in result['updated']] == [good.name]
    assert [r['lead_id'] for r in result['failed']] == [bad.name]
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize('identifier', ['missing', '../outside.json', 'done', 'other'])
def test_reject_invalid_targets(cfg, identifier):
    lead(cfg, 'done', approved=False)
    lead(cfg, 'other', kind='company.update')
    with pytest.raises(ValueError):
        rescore.rescore_lead(cfg, None, identifier)


def test_ambiguous_stem(cfg):
    lead(cfg)
    shadow = cfg.workspace_dir / '11-shadow'
    shadow.mkdir()
    (shadow / 'one.json').write_text((cfg.workspace_dir / '12-intake/one.json').read_text())
    with pytest.raises(ValueError, match='found 2'):
        rescore.resolve_lead(cfg, 'one')


def test_saved_text_and_attachment(cfg):
    path = lead(cfg, raw_text='complete saved JD')
    assert rescore._saved_jd(cfg, json.loads(path.read_text())) == (
        'complete saved JD', 'saved original text')
    attachment = cfg.workspace_dir / '12-intake/jd.txt'
    attachment.write_text('attachment JD')
    proposal = json.loads(lead(cfg, jd_file='12-intake/jd.txt').read_text())
    assert rescore._saved_jd(cfg, proposal) == ('attachment JD', 'saved attachment')
    proposal['jd_file'] = '/etc/passwd'
    assert rescore._saved_jd(cfg, proposal)[0] == 'saved JD'
    del proposal['lead']['jd_excerpt']
    proposal['raw_excerpt'] = 'legacy excerpt'
    assert rescore._saved_jd(cfg, proposal)[0] == 'legacy excerpt'


@pytest.mark.parametrize('action', ['edit', 'approve', 'retire'])
def test_concurrent_changes_not_overwritten(cfg, action):
    path = lead(cfg)
    class Concurrent:
        def generate(self, *a, **kw):
            if action == 'retire':
                path.unlink()
            else:
                data = json.loads(path.read_text())
                data['approved' if action == 'approve' else 'metadata'] = True
                path.write_text(json.dumps(data))
            return '{"fit": 4}'
    with pytest.raises(ValueError):
        rescore.rescore_lead(cfg, Concurrent(), path.stem)
    assert not list(path.parent.glob('.rescore-*'))
    if path.exists():
        assert json.loads(path.read_text())['fit']['fit'] == 2


def test_cli_single_batch_empty_and_failures(cfg, monkeypatch, capsys):
    monkeypatch.setattr('joblander.llm.from_config', lambda *a: Model())
    assert main(['rescore', '--all-new']) == 0
    assert 'No New Leads' in capsys.readouterr().out
    path = lead(cfg)
    assert main(['rescore', path.stem]) == 0
    assert 'limited context' in capsys.readouterr().out
    lead(cfg, 'bad', lead={})
    assert main(['rescore', '--all-new']) == 1
    assert main(['rescore', 'missing']) == 1


@pytest.mark.parametrize('args', [[], ['one', '--all-new']])
def test_cli_requires_one_target(args):
    with pytest.raises(SystemExit) as exc:
        main(['rescore', *args])
    assert exc.value.code == 2


def test_intake_preserves_original_text(cfg, monkeypatch):
    from joblander.scout import intake
    from joblander.llm import MockLLM
    (cfg.workspace_dir / '09-projections').mkdir()
    (cfg.workspace_dir / '09-projections/tracker.json').write_text('{"rows": []}')
    monkeypatch.setattr(sourcing, 'assess_fit', lambda *a, **kw: {'fit': 3})
    raw = 'Original JD\n' + 'hard requirement ' * 100
    model = MockLLM([json.dumps({'company': 'New', 'position': 'Engineer',
                                 'comp_mentions': ['salary disclosed']})])
    path = intake(cfg, model, raw)
    assert json.loads(path.read_text())['raw_text'] == raw


def test_symlink_outside_rejected(cfg, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.json')
    outside.write_text(json.dumps({'kind': 'lead.intake', 'approved': None, 'lead': {}}))
    (cfg.workspace_dir / '12-intake/link.json').symlink_to(outside)
    with pytest.raises(ValueError):
        rescore.resolve_lead(cfg, 'link')


def test_empty_cli_never_initializes_model(cfg, monkeypatch):
    def unexpected(*args):
        raise AssertionError('No model needed for empty batch')
    monkeypatch.setattr('joblander.llm.from_config', unexpected)
    assert main(['rescore', '--all-new']) == 0


def test_cli_batch_live_progress_and_counts(cfg, monkeypatch, capsys):
    import builtins
    from joblander.llm import LLMError

    failed = lead(cfg, 'z-failed', lead={'company': 'Failure Co', 'position': 'Engineer'})
    unchanged = lead(cfg, 'y-unchanged', fit={'fit': 4, 'why': 'old explanation'})
    changed = lead(cfg, 'x-changed', lead={'company': 'UNISYNC',
                                          'position': 'Big Data Engineer'}, fit={'fit': 3})
    unscored = lead(cfg, 'w-unscored', fit={})
    before = failed.read_bytes()
    chunks = []
    flush_values = []
    original_print = builtins.print

    def track_print(*args, **kwargs):
        flush_values.append(kwargs.get('flush', False))
        original_print(*args, **kwargs)

    class LiveModel:
        def generate(self, prompt, **kwargs):
            # These lines must already be visible while the model is running.
            output = capsys.readouterr().out
            chunks.append(output)
            index = len(chunks)
            assert f'Rescoring {index}/4:' in output
            assert 'Old score:' in output
            if index == 1:
                assert 'Failure Co — Engineer...' in output
                raise LLMError('temporary model failure')
            if index == 2:
                assert 'Failed: temporary model failure' in output
            if index == 3:
                assert 'UNISYNC — Big Data Engineer...' in output
                assert 'Old score: 3/5' in output
            if index == 4:
                assert 'Old score: Unscored' in output
            return json.dumps({'fit': 4, 'why': 'changed explanation', 'req_gaps': []})

    monkeypatch.setattr(builtins, 'print', track_print)
    monkeypatch.setattr('joblander.llm.from_config', lambda *args: LiveModel())
    assert main(['rescore', '--all-new']) == 1
    output = ''.join(chunks) + capsys.readouterr().out
    assert output.count('New score: 4/5') == 3
    assert output.endswith('Rescored: 4\nChanged: 2\nUnchanged: 1\nFailed: 1\n')
    assert flush_values and all(flush_values)
    assert failed.read_bytes() == before
    for path in [unchanged, changed, unscored]:
        assert json.loads(path.read_text())['fit']['fit'] == 4
    assert json.loads(unchanged.read_text())['fit']['why'] == 'changed explanation'


def test_batch_callback_snapshot_and_silent_default(cfg, capsys):
    first = lead(cfg, 'z-first')
    lead(cfg, 'a-second', fit={'fit': 4})
    events = []

    class AddsLead(Model):
        def generate(self, prompt, **kwargs):
            if not (cfg.workspace_dir / '12-intake/new-arrival.json').exists():
                assert events[-1]['status'] == 'start'
                assert events[-1]['lead_id'] == first.name
                lead(cfg, 'new-arrival')
            return super().generate(prompt, **kwargs)

    result = rescore.rescore_all_new(cfg, AddsLead(), progress=events.append)
    assert [e['status'] for e in events] == ['start', 'success', 'start', 'success']
    assert [e['index'] for e in events] == [1, 1, 2, 2]
    assert all(e['total'] == 2 for e in events)
    assert events[0]['old_score'] == 2 and events[1]['new_score'] == 4
    assert result['summary'] == {'rescored': 2, 'changed': 1, 'unchanged': 1, 'failed': 0}
    assert json.loads((cfg.workspace_dir / '12-intake/new-arrival.json').read_text())['fit']['fit'] == 2
    rescore.rescore_all_new(cfg, Model())
    assert capsys.readouterr().out == ''


def test_batch_progress_handles_lead_removed_after_snapshot(cfg, monkeypatch):
    removed = lead(cfg, 'z-removed')
    good = lead(cfg, 'a-good')
    original_pending = rescore.pending_leads
    snapshot = original_pending(cfg)
    removed.unlink()
    calls = 0

    def pending(cfg):
        nonlocal calls
        calls += 1
        return snapshot if calls == 1 else original_pending(cfg)

    monkeypatch.setattr(rescore, 'pending_leads', pending)
    events = []
    result = rescore.rescore_all_new(cfg, Model(), progress=events.append)
    assert [e['status'] for e in events] == ['start', 'failed', 'start', 'success']
    assert events[0]['company'] == 'Unknown company'
    assert result['updated'][0]['lead_id'] == good.name
    assert result['summary'] == {'rescored': 2, 'changed': 1, 'unchanged': 0, 'failed': 1}


def test_empty_batch_cli_summary(cfg, monkeypatch, capsys):
    def unexpected(*args):
        raise AssertionError('Empty batch must not initialize the model')
    monkeypatch.setattr('joblander.llm.from_config', unexpected)
    assert main(['rescore', '--all-new']) == 0
    assert capsys.readouterr().out == (
        'No New Leads to rescore\n\nRescored: 0\nChanged: 0\nUnchanged: 0\nFailed: 0\n')


def test_single_cli_keeps_json_output(cfg, monkeypatch, capsys):
    path = lead(cfg)
    monkeypatch.setattr('joblander.llm.from_config', lambda *args: Model())
    assert main(['rescore', path.stem]) == 0
    assert json.loads(capsys.readouterr().out) == {
        'lead_id': path.name, 'fit': 4, 'context': 'saved excerpt (limited context)'}
