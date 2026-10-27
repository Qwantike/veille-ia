#!/usr/bin/env python3
"""Veille IA : flux RSS -> scoring -> page HTML statique du jour.

Usage :
    python veille.py                 génère docs/veille-AAAA-MM-JJ.html
    python veille.py --report 14     bilan des sources sur les 14 derniers jours
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
import time
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
UA = "Mozilla/5.0 (compatible; veille-perso/1.0)"

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]


# --------------------------------------------------------------------- modèle

@dataclass
class Article:
    title: str
    url: str
    source: str
    category: str
    published: datetime
    summary: str
    text: str                      # titre + résumé complet, pour le matching
    points: int = 0                # points Hacker News si disponibles
    discussion: str = ""           # lien vers les commentaires (HN)
    score: float = 0.0
    hits: list[str] = field(default_factory=list)
    kw_score: float = 0.0


# --------------------------------------------------------------------- outils

def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_url(u: str) -> str:
    p = urlsplit(u.strip())
    q = [(k, v) for k, v in parse_qsl(p.query)
         if not k.startswith("utm_") and k not in {"ref", "fbclid", "gclid"}]
    host = p.netloc.lower().removeprefix("www.")
    return urlunsplit((p.scheme.lower(), host, p.path.rstrip("/"), urlencode(q), ""))


def norm_title(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", t.lower()))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def date_fr(iso: str, weekday: bool = True) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    base = f"{d.day} {MOIS[d.month - 1]} {d.year}"
    return f"{JOURS[d.weekday()]} {base}" if weekday else base


def age_label(pub: datetime, now: datetime) -> str:
    h = (now - pub).total_seconds() / 3600
    if h < 1:
        return "il y a moins d'une heure"
    if h < 24:
        return f"il y a {int(h)} h"
    return "hier" if h < 48 else f"il y a {int(h // 24)} j"


# --------------------------------------------------------------------- collecte

def fetch(src: dict, timeout: int):
    err = None
    for attempt in range(3):            # 3 essais : certains sites répondent 429/503 en rafale
        try:
            r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code in (429, 503) and attempt < 2:
                wait = r.headers.get("Retry-After", "")
                time.sleep(min(int(wait) if wait.isdigit() else 4 * (attempt + 1), 15))
                continue
            r.raise_for_status()
            return src, feedparser.parse(r.content).entries, None
        except Exception as e:          # un flux en panne ne doit jamais bloquer la veille
            err = f"{type(e).__name__}: {str(e)[:100]}"
    return src, [], err


def to_articles(src: dict, entries, now: datetime, window_hours: int) -> list[Article]:
    out = []
    for e in entries:
        title = strip_html(e.get("title", ""))
        url = e.get("link", "")
        if not title or not url:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime(*ts[:6], tzinfo=timezone.utc) if ts else now
        pub = min(pub, now)
        if (now - pub).total_seconds() > window_hours * 3600:
            continue
        raw = e.get("summary") or ""
        if not raw and e.get("content"):
            raw = e["content"][0].get("value", "")
        full = strip_html(raw)
        m = re.search(r"Points:\s*(\d+)", full)
        summary = re.sub(r"\s*(Article URL|Comments URL|Points:|# Comments:).*$", "", full)
        out.append(Article(
            title=title, url=url, source=src["name"], category=src["category"],
            published=pub, summary=summary[:320].rstrip(),
            text=f"{title}\n{full[:2500]}",
            points=int(m.group(1)) if m else 0,
            discussion=e.get("comments", ""),
        ))
    return out


# --------------------------------------------------------------------- scoring

def compile_keywords(kw: dict):
    return [(str(term), w, re.compile(r"(?<!\w)" + re.escape(str(term)) + r"(?:s|es)?(?!\w)", re.I))
            for term, w in kw.items()]


def score_article(a: Article, src: dict, kws, now: datetime) -> None:
    kw = 0.0
    for term, w, pat in kws:
        in_title, in_body = pat.search(a.title), pat.search(a.text)
        if in_title or in_body:
            if w > 0:
                a.hits.append(term)
            kw += w * (2 if in_title else 1)
    a.kw_score = kw
    kw_part = max(min(kw, 12), -20)                     # plafonné pour ne pas écraser le reste
    weight_part = src.get("weight", 3) * 2              # 2 à 10
    age_h = (now - a.published).total_seconds() / 3600
    recency = 4 * 0.5 ** (age_h / 24)                   # 4 points à t=0, divisés par 2 toutes les 24 h
    points = min(a.points / 100, 3)                     # bonus Hacker News
    a.score = round(weight_part + kw_part + recency + points, 2)


def dedupe(sorted_articles: list[Article], threshold: float) -> list[Article]:
    kept, urls, titles = [], set(), []
    for a in sorted_articles:                           # déjà triés par score décroissant
        u, t = norm_url(a.url), norm_title(a.title)
        if u in urls or any(difflib.SequenceMatcher(None, t, k).ratio() >= threshold for k in titles):
            continue
        urls.add(u)
        titles.append(t)
        kept.append(a)
    return kept


def select(arts: list[Article], s: dict) -> list[Article]:
    chosen, per_src, per_cat = [], Counter(), Counter()
    for a in arts:
        if a.score < s["min_score"]:
            break
        if per_src[a.source] >= s["max_per_source"] or per_cat[a.category] >= s["max_per_category"]:
            continue
        chosen.append(a)
        per_src[a.source] += 1
        per_cat[a.category] += 1
        if len(chosen) == s["max_articles"]:
            break
    return chosen


# --------------------------------------------------------------------- rendu

CSS = """
:root{
  --bg:#f3f5f2; --ink:#1b2220; --muted:#5b6762; --rule:#d3dad6; --link:#0a5c8f;
  --labs:#2f5fd0; --open:#1d8a5a; --agents:#b8572a; --entreprise:#7a4fb0;
  --ml_data:#a8841a; --recherche:#c0392b; --communaute:#5f6f7a;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#121715; --ink:#e4eae6; --muted:#97a39d; --rule:#2b3531; --link:#6cb6ec;
  --labs:#7da2ff; --open:#4fcf93; --agents:#f09a6c; --entreprise:#b99aea;
  --ml_data:#e0bd52; --recherche:#f2877a; --communaute:#9fb0bb;
}}
*{box-sizing:border-box}
html{background:var(--bg);color:var(--ink)}
body{margin:0 auto;max-width:44rem;padding:3rem 1.25rem 4rem;
  font:17px/1.55 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif}
a{color:inherit}
header{margin-bottom:2.5rem}
header h1{font-size:2.6rem;line-height:1.1;font-weight:600;margin:0 0 .6rem;letter-spacing:-.015em}
header p{margin:0;color:var(--muted);font:.95rem/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header nav{margin-top:.8rem;font:.9rem system-ui,sans-serif}
header nav a{color:var(--link)}
article{border-left:4px solid var(--c,var(--rule));padding:.1rem 0 .1rem 1rem;margin:0 0 1.9rem}
article h2{font-size:1.28rem;line-height:1.3;font-weight:600;margin:0 0 .25rem}
article h2 a{text-decoration:none}
article h2 a:hover{text-decoration:underline;text-underline-offset:.15em}
.meta{font:.85rem/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--muted);
  display:flex;flex-wrap:wrap;gap:.2rem 1rem;margin-bottom:.4rem}
.meta .cat{color:var(--c);font-weight:600}
article p{margin:0;color:var(--ink);opacity:.86;font-size:.97rem}
.why{margin-top:.4rem;font:.8rem system-ui,sans-serif;color:var(--muted)}
.empty{padding:1rem 0;color:var(--muted)}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--rule);
  font:.85rem/1.5 system-ui,sans-serif;color:var(--muted)}
footer summary{cursor:pointer}
footer ul{margin:.5rem 0 0;padding-left:1.1rem}
.days{list-style:none;padding:0;margin:0}
.days li{padding:.55rem 0;border-bottom:1px solid var(--rule);display:flex;gap:1rem;justify-content:space-between}
.days a{text-decoration:none;font-size:1.1rem}
.days a:hover{text-decoration:underline}
.days span{font:.85rem system-ui,sans-serif;color:var(--muted)}
a:focus-visible,summary:focus-visible{outline:2px solid var(--link);outline-offset:3px}
@media (max-width:520px){header h1{font-size:2rem}}
"""

PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def e(s: str) -> str:
    return html.escape(s, quote=True)


def render_day(today: str, chosen: list[Article], cats: dict, total: int, failed: list[tuple[str, str]],
               ok_count: int, now: datetime) -> str:
    items = []
    for a in chosen:
        why = ", ".join(a.hits[:5])
        disc = f' <a href="{e(a.discussion)}">discussion</a>' if a.discussion else ""
        pts = f"<span>{a.points} points</span>" if a.points else ""
        items.append(f"""<article style="--c:var(--{e(a.category)})">
  <h2><a href="{e(a.url)}">{e(a.title)}</a></h2>
  <div class="meta"><span class="cat">{e(cats.get(a.category, a.category))}</span><span>{e(a.source)}</span><span>{age_label(a.published, now)}</span>{pts}{disc}</div>
  {f"<p>{e(a.summary)}</p>" if a.summary else ""}
  {f'<div class="why">Mots-clés : {e(why)}</div>' if why else ""}
</article>""")
    if not items:
        items.append('<p class="empty">Aucun article n\'a atteint le score minimum aujourd\'hui. '
                     "Baisse <code>min_score</code> ou ajoute des sources dans <code>config.yaml</code>.</p>")
    errors = ""
    if failed:
        lis = "".join(f"<li>{e(n)} : {e(m)}</li>" for n, m in failed)
        errors = f"<details><summary>{len(failed)} flux en erreur</summary><ul>{lis}</ul></details>"
    head = f"""<header>
  <h1>{e(date_fr(today))}</h1>
  <p>{len(chosen)} articles retenus sur {total} candidats, {ok_count} flux lus.</p>
  <nav><a href="index.html">Jours précédents</a></nav>
</header>"""
    foot = f"<footer><p>Généré à {now.astimezone(ZoneInfo('Europe/Paris')):%H:%M}.</p>{errors}</footer>"
    return PAGE.format(title=f"Veille IA, {date_fr(today, False)}", css=CSS,
                       body=head + "\n<main>\n" + "\n".join(items) + "\n</main>\n" + foot)


def render_index(out: Path) -> None:
    days = sorted((m.group(1) for p in out.glob("veille-*.html")
                   if (m := re.fullmatch(r"veille-(\d{4}-\d{2}-\d{2})\.html", p.name))), reverse=True)
    rows = "".join(
        f'<li><a href="veille-{d}.html">{e(date_fr(d, False))}</a><span>{JOURS[datetime.strptime(d, "%Y-%m-%d").weekday()]}</span></li>'
        for d in days) or '<li>Aucune page pour l\'instant.</li>'
    body = f'<header><h1>Veille IA</h1><p>{len(days)} jours archivés.</p></header>\n<main><ul class="days">{rows}</ul></main>'
    (out / "index.html").write_text(PAGE.format(title="Veille IA", css=CSS, body=body), encoding="utf-8")


# --------------------------------------------------------------------- bilan des sources

def report(state: Path, days: int) -> None:
    lines = [json.loads(l) for l in (state / "stats.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = lines[-days:]
    agg: dict[str, Counter] = {}
    for run in lines:
        for name, st in run["sources"].items():
            c = agg.setdefault(name, Counter())
            c["candidates"] += st["candidates"]
            c["kept"] += st["kept"]
            c["errors"] += 1 if st["error"] else 0
    print(f"Bilan sur {len(lines)} jour(s)\n")
    print(f"{'source':36} {'candidats':>9} {'retenus':>8} {'erreurs':>8}")
    for name, c in sorted(agg.items(), key=lambda kv: (-kv[1]["kept"], kv[0])):
        flag = "  <- à revoir" if c["kept"] == 0 or c["errors"] else ""
        print(f"{name[:36]:36} {c['candidates']:>9} {c['kept']:>8} {c['errors']:>8}{flag}")


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    ap.add_argument("--out", type=Path, default=ROOT / "docs")
    ap.add_argument("--state", type=Path, default=ROOT / "state")
    ap.add_argument("--report", type=int, metavar="JOURS", help="affiche le bilan des sources")
    args = ap.parse_args()

    if args.report:
        report(args.state, args.report)
        return

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    s, cats, sources = cfg["settings"], cfg["categories"], cfg["sources"]
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(s["timezone"])).date().isoformat()
    args.out.mkdir(parents=True, exist_ok=True)
    args.state.mkdir(parents=True, exist_ok=True)

    # 1. collecte en parallèle
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda src: fetch(src, s["timeout_seconds"]), sources))

    # 2. articles déjà montrés un jour précédent : exclus (ceux d'aujourd'hui restent, pour relancer sans effet de bord)
    seen_path = args.state / "seen.json"
    seen: dict[str, str] = load_json(seen_path, {})
    kws = compile_keywords(cfg["keywords"])

    candidates, stats, failed = [], {}, []
    for src, entries, err in results:
        arts = [a for a in to_articles(src, entries, now, s["window_hours"])
                if seen.get(norm_url(a.url), today) == today]
        for a in arts:
            score_article(a, src, kws, now)
        arts = [a for a in arts if a.kw_score >= src.get("min_keyword_score", 0)]
        stats[src["name"]] = {"candidates": len(arts), "kept": 0, "error": err}
        if err:
            failed.append((src["name"], err))
        candidates += arts

    # 3. tri, dédoublonnage, sélection
    candidates.sort(key=lambda a: a.score, reverse=True)
    unique = dedupe(candidates, s["similarity_threshold"])
    chosen = select(unique, s)
    for a in chosen:
        stats[a.source]["kept"] += 1
        seen[norm_url(a.url)] = today

    # 4. rendu
    page = render_day(today, chosen, cats, len(unique), failed, len(sources) - len(failed), now)
    (args.out / f"veille-{today}.html").write_text(page, encoding="utf-8")
    render_index(args.out)

    # 5. état : articles vus (purgés après 45 j) et statistiques par source
    cutoff = (now.date().toordinal() - 45)
    seen = {u: d for u, d in seen.items() if datetime.strptime(d, "%Y-%m-%d").toordinal() >= cutoff}
    seen_path.write_text(json.dumps(seen, indent=0), encoding="utf-8")
    stats_path = args.state / "stats.jsonl"
    old = [l for l in (stats_path.read_text(encoding="utf-8").splitlines() if stats_path.exists() else [])
           if l.strip() and json.loads(l)["date"] != today]
    old.append(json.dumps({"date": today, "sources": stats}, ensure_ascii=False))
    stats_path.write_text("\n".join(old[-120:]) + "\n", encoding="utf-8")

    print(f"{len(chosen)} articles retenus sur {len(unique)} candidats, "
          f"{len(failed)} flux en erreur -> {args.out / f'veille-{today}.html'}")


if __name__ == "__main__":
    main()
