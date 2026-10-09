"""Japan Dev / TokyoDev 公开板：解析（真实页面结构的精简样本）+ 与 MCF 同一条查重/评分/入池管线。"""

import json
from pathlib import Path

from joblander import sourcing as S
from joblander.config import Config
from joblander.llm import MockLLM

FX = Path(__file__).parent / "fixtures"

SITEMAP = ('<urlset><url><loc>https://japan-dev.com/jobs</loc></url>'
           '<url><loc>https://japan-dev.com/backend-jobs-in-japan</loc></url>'
           '<url><loc>https://japan-dev.com/jobs/acme/acme-swe-1ab2cd</loc></url>'
           '<url><loc>https://japan-dev.com/blog/x</loc></url></urlset>')


def test_parse_tokyodev_cards():
    cards = S.parse_tokyodev_cards((FX / "tokyodev_jobs.html").read_text(encoding="utf-8"))
    assert len(cards) == 4
    c = next(c for c in cards if c["title"] == "Senior Quality Automation Engineer / SDET")
    assert c["company"] == "VISASQ"
    assert c["salary"] == "¥7M ~ ¥12M"
    assert c["jp"] == "No Japanese required"
    assert c["abroad"] == "Japan residents only"
    assert c["skills"] == ["Quality Assurance", "Test Automation"]
    assert c["url"] == "https://www.tokyodev.com/companies/visasq/jobs/senior-quality-automation-engineer-sdet"
    no_sal = next(c for c in cards if c["company"] == "PayPay Card Corporation")
    assert no_sal["salary"] == "" and no_sal["remote"]
    lead = S.tokyodev_to_lead(c, lang="en")
    assert lead["company"] == "VISASQ" and lead["location"] == "Japan"
    assert lead["comp_mentions"] == ["¥7M ~ ¥12M JPY/yr"]
    assert "Japan residents only" in lead["highlight"] and "No Japanese required" in lead["highlight"]


def test_parse_japandev_detail():
    d = S.parse_japandev_detail((FX / "japandev_detail.html").read_text(encoding="utf-8"))
    assert d["title"] == "Senior DevOps Engineer - Build Systems & CI"
    assert d["company"] == "Mujin"
    assert d["posted"] == "2026-06-08"
    assert d["location"] == "Tokyo"
    assert d["salary"] == "¥10M ~ ¥13M"
    assert "Go" in d["skills"] and "Kubernetes" in d["skills"]
    assert d["description"].startswith("Mujin creates")
    lead = S.japandev_to_lead("https://japan-dev.com/jobs/mujin/x", d, lang="en")
    assert lead["location"] == "Tokyo, Japan"
    assert lead["comp_mentions"] == ["¥10M ~ ¥13M JPY/yr"]
    assert lead["jd_excerpt"].startswith("Mujin creates")


def test_japandev_job_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_li_get", lambda url, timeout=20: SITEMAP)
    assert S.japandev_job_urls() == ["https://japan-dev.com/jobs/acme/acme-swe-1ab2cd"]


def _cfg(tmp_path, extra_prefs="", features=None):
    ws = tmp_path / "ws"
    (ws / "09-projections").mkdir(parents=True)
    (ws / "09-projections" / "tracker.json").write_text(json.dumps(
        {"rows": [{"Company": "Tracked Ltd", "Status": "Applied"}]}), encoding="utf-8")
    (ws / "02-targets").mkdir()
    (ws / "02-targets" / "sourcing-prefs.yaml").write_text(
        "keywords: [software engineer]\nlocations: [Singapore]\nexclude: [intern]\n" + extra_prefs,
        encoding="utf-8")
    return Config(raw={"workspace_dir": str(ws), "features": features or {}},
                  path=tmp_path / "c.yaml")


TD_CARDS = [
    {"uid": "/companies/newco/jobs/swe-1", "title": "Backend Engineer", "company": "NewCo",
     "salary": "¥8M ~ ¥12M", "location": "Japan", "jp": "No Japanese required",
     "abroad": "Apply from abroad", "remote": "Partially remote",
     "skills": ["Go"], "url": "https://www.tokyodev.com/companies/newco/jobs/swe-1"},
    {"uid": "/companies/newco/jobs/swe-2", "title": "Backend Engineer", "company": "NewCo",
     "salary": "", "location": "Japan", "jp": "", "abroad": "", "remote": "",
     "skills": [], "url": "https://www.tokyodev.com/companies/newco/jobs/swe-2"},  # 同轮重复
    {"uid": "/companies/x/jobs/intern-1", "title": "Software Intern", "company": "X",
     "salary": "", "location": "Japan", "jp": "", "abroad": "", "remote": "",
     "skills": [], "url": "https://www.tokyodev.com/companies/x/jobs/intern-1"},   # 排除词
    {"uid": "/companies/tracked/jobs/swe-3", "title": "SWE", "company": "Tracked Ltd",
     "salary": "", "location": "Japan", "jp": "", "abroad": "", "remote": "",
     "skills": [], "url": "https://www.tokyodev.com/companies/tracked/jobs/swe-3"},  # 已在库
]


def test_tokyodev_pipeline(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(S, "tokyodev_search", lambda kw, limit=40, timeout=30: TD_CARDS)
    monkeypatch.setattr(S, "assess_fit", lambda cfg, llm, lead, jd_text="": {"fit": 4})
    outs = S.source_tokyodev(cfg, MockLLM([]))
    assert len(outs) == 1
    prop = json.loads(outs[0].read_text(encoding="utf-8"))
    assert prop["source_hint"] == "tokyodev" and prop["lead"]["company"] == "NewCo"
    assert prop["fit"]["fit"] == 4
    assert S.source_tokyodev(cfg, MockLLM([])) == []            # 幂等：见过的不再提


def test_japandev_pipeline(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    detail = (FX / "japandev_detail.html").read_text(encoding="utf-8")
    pages = {"https://japan-dev.com/jobs/acme/acme-new-aaaaaa": detail,
             "https://japan-dev.com/jobs/acme/acme-old-bbbbbb": detail.replace("2026-06-08", "2020-01-01"),
             "https://japan-dev.com/jobs/acme/acme-bad-cccccc": "<html>no ld json</html>"}
    monkeypatch.setattr(S, "japandev_job_urls", lambda timeout=20: list(pages))
    monkeypatch.setattr(S, "_li_get", lambda url, timeout=20: pages[url])
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(S, "assess_fit", lambda cfg, llm, lead, jd_text="": {"fit": 4})
    outs = S.source_japandev(cfg, MockLLM([]), days=400)        # 2026-06-08 得在窗口内
    assert len(outs) == 1                                       # 旧岗记账跳过、坏页 skip
    prop = json.loads(outs[0].read_text(encoding="utf-8"))
    assert prop["source_hint"] == "japandev" and prop["lead"]["company"] == "Mujin"
    assert prop["lead"]["jd_excerpt"].startswith("Mujin creates")
    seen = json.loads((cfg.workspace_dir / "19-sourcing" / "japandev-seen.json")
                      .read_text(encoding="utf-8"))
    assert seen["https://japan-dev.com/jobs/acme/acme-old-bbbbbb"] == "2020-01-01"
    assert seen["https://japan-dev.com/jobs/acme/acme-bad-cccccc"] == "skip"
    assert S.source_japandev(cfg, MockLLM([]), days=400) == []   # 幂等：sitemap 全在账上


def test_jp_off_or_blocked_never_raises(tmp_path, monkeypatch):
    off = {"tokyodev": False, "japandev": False}
    cfg_a = _cfg(tmp_path / "a", features=off)
    assert S.source_tokyodev(cfg_a, MockLLM([])) == []
    assert S.source_japandev(cfg_a, MockLLM([])) == []
    cfg = _cfg(tmp_path / "b")

    def blocked(*a, **k):
        raise OSError("403 Forbidden")
    monkeypatch.setattr(S, "tokyodev_search", blocked)
    monkeypatch.setattr(S, "japandev_job_urls", blocked)
    monkeypatch.setattr(S, "source_mcf", lambda cfg, llm, days=2, **k: [])
    monkeypatch.setattr(S, "source_linkedin", lambda cfg, llm, days=2, **k: [])
    assert S.source_tokyodev(cfg, MockLLM([])) == []
    assert S.source_japandev(cfg, MockLLM([])) == []
    assert S.source_all(cfg, MockLLM([])) == {"mcf": 0, "linkedin": 0, "tokyodev": 0, "japandev": 0, "targets": 0}
