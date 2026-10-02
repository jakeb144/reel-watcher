"""Render an advice library JSON to one static, self-contained HTML page.

Deterministic: same input, same output. Stdlib only. The JSON is built from your
study files (see prompts/build_advice_library.md and examples/advice_library.json).

    reel-watcher advice --in library.json --out advice-library.html
"""
import argparse
import html
import json
import sys

STRENGTH_RULE = (
    "Strength is mechanical. Strong: 3 or more independent creators, or 2 or more "
    "creators who show results or cite data. Weak: a single creator whose evidence is "
    "opinion or selling, or whose best item is non-specific. Everything else is medium."
)
EVIDENCE = {
    "creator_shows_results": "shows results",
    "cites_data": "cites data",
    "claims_results": "claims results",
    "opinion": "opinion",
    "selling": "selling",
}
RANK = {"strong": 0, "medium": 1, "weak": 2}

CSS = """
:root{--bg:#f6f5f2;--fg:#1b1b1a;--mut:#6b6a66;--line:#dddad3;--card:#efede8;--acc:#1f5f8b}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--fg:#ecebe7;--mut:#9a9890;--line:#2e2d2a;--card:#1d1d1b;--acc:#7fb2d6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,sans-serif;overflow-wrap:anywhere}
main{max-width:820px;margin:0 auto;padding:28px 16px 80px}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:18px;margin:36px 0 10px;scroll-margin-top:64px}
p{margin:0 0 10px}
.sub{color:var(--mut)}
a{color:var(--acc)}
nav{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);margin:0 -16px;padding:8px 16px;overflow-x:auto;white-space:nowrap;font-size:14px}
nav a{margin-right:14px;text-decoration:none}
.tools{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;margin:16px 0 4px;font-size:14px}
.tools input[type=search]{flex:1 1 220px;min-width:0;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.tools label{display:inline-flex;gap:5px;align-items:center;min-height:32px}
.p{background:var(--card);border-radius:8px;padding:14px 14px 12px;margin:0 0 10px}
.p h3{font-size:16px;margin:0 0 4px;font-weight:650}
.meta{font-size:14px;color:var(--mut);margin:0 0 8px}
.meta b{color:var(--fg)}
.detail p{margin:0 0 6px;font-size:15px}
.fj{font-size:14px;color:var(--mut)}
details{margin-top:6px;font-size:14px}
summary{cursor:pointer;color:var(--acc);min-height:28px}
.src{margin:8px 0 0;padding:0;list-style:none}
.src li{padding:6px 0}
.q{color:var(--mut)}
.imp{display:flex;gap:8px;align-items:center;font-size:14px;margin-top:8px;min-height:32px}
.cf li{margin-bottom:8px}
.hide{display:none}
.note{display:block;width:100%;min-height:40px;margin-top:6px;font:inherit;font-size:14px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);resize:vertical}
#copy{position:fixed;right:16px;bottom:16px;z-index:9;font:inherit;font-size:15px;padding:10px 14px;border:0;border-radius:8px;background:var(--acc);color:var(--bg);cursor:pointer}
#out{display:none;width:100%;min-height:160px;margin:16px 0;font:13px/1.4 ui-monospace,monospace}
#none{display:none;color:var(--mut)}
"""

JS = """
var items=[].slice.call(document.querySelectorAll('.p'));
var q=document.getElementById('q'),st=document.getElementsByName('st');
function lsGet(k){try{return localStorage.getItem(k)}catch(e){return null}}
function lsSet(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function sel(){for(var i=0;i<st.length;i++)if(st[i].checked)return st[i].value;return 'all'}
function apply(){
  var s=sel(),t=q.value.toLowerCase().trim(),shown=0;
  items.forEach(function(el){
    var ok=true,cb=el.querySelector('input[type=checkbox]');
    if(s==='ticked')ok=cb.checked;else if(s!=='all')ok=el.dataset.strength===s;
    if(ok&&t)ok=el.dataset.text.indexOf(t)>-1;
    el.classList.toggle('hide',!ok);if(ok)shown++;
  });
  [].slice.call(document.querySelectorAll('section.bk')).forEach(function(b){
    b.classList.toggle('hide',!b.querySelector('.p:not(.hide)'));
  });
  document.getElementById('none').style.display=shown?'none':'block';
}
items.forEach(function(el){
  var cb=el.querySelector('input[type=checkbox]');
  cb.checked=lsGet('al-'+el.id)==='1';
  cb.addEventListener('change',function(){lsSet('al-'+el.id,cb.checked?'1':'0');apply()});
});
items.forEach(function(el){
  var n=el.querySelector('.note');n.value=lsGet('aln-'+el.id)||'';
  n.addEventListener('input',function(){lsSet('aln-'+el.id,n.value)});
});
document.getElementById('copy').addEventListener('click',function(){
  var lines=['ADVICE LIBRARY PICKS'],t=0,nn=0;
  items.forEach(function(el){
    var on=el.querySelector('input[type=checkbox]').checked,n=el.querySelector('.note').value.trim();
    if(!on&&!n)return;
    if(on)t++;if(n)nn++;
    lines.push((on?'[x] ':'[ ] ')+el.id+' ('+el.dataset.strength+'): '+el.querySelector('h3').textContent+(n?'\\n    note: '+n.replace(/\\n/g,' '):''));
  });
  lines.splice(1,0,t+' ticked, '+nn+' notes');
  var txt=lines.join('\\n'),o=document.getElementById('out'),b=this;
  o.value=txt;o.style.display='block';
  function done(){b.textContent='Copied '+t+' picks';setTimeout(function(){b.textContent='Copy my picks'},2500)}
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt).then(done,function(){o.select();try{document.execCommand('copy')}catch(e){}done()})}
  else{o.select();try{document.execCommand('copy')}catch(e){}done()}
});
q.addEventListener('input',apply);
for(var i=0;i<st.length;i++)st[i].addEventListener('change',apply);
apply();
"""


def e(s):
    return html.escape("" if s is None else str(s), quote=True)


def views(n):
    n = n or 0
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1000:
        return "%dK" % round(n / 1000)
    return str(n)


def link(code):
    return "https://www.instagram.com/reel/%s/" % code


def principle(p):
    srcs = []
    for s in p["sources"]:
        a = s.get("author") or "unknown"
        who = "@" + e(a) if a != "unknown" else "unknown author"
        q = s.get("quote") or ""
        srcs.append(
            '<li><a href="%s" target="_blank" rel="noopener">%s</a>, %s views, %s'
            '<div class="q">%s</div></li>'
            % (e(link(s["code"])), who, views(s.get("views")),
               e(EVIDENCE.get(s.get("evidence"), s.get("evidence") or "unrated")),
               e(q))
        )
    det = "".join("<p>%s</p>" % e(l) for l in (p.get("detail") or "").split("\n") if l.strip())
    fj = '<p class="fj">For the user: %s</p>' % e(p["note"]) if p.get("note") else ""
    cf = ""
    if p.get("conflicts_with"):
        cf = '<p class="meta">Conflicts with: %s</p>' % ", ".join(
            '<a href="#%s">%s</a>' % (e(c), e(c)) for c in p["conflicts_with"])
    text = " ".join([p["principle"], p.get("detail") or "", p.get("note") or ""]
                    + [(s.get("author") or "") + " " + (s.get("quote") or "") for s in p["sources"]]).lower()
    return (
        '<article class="p" id="%s" data-strength="%s" data-text="%s">'
        '<h3>%s</h3>'
        '<p class="meta"><b>%s</b>, %d creator%s, %d item%s, best evidence: %s</p>'
        '<div class="detail">%s</div>%s%s'
        '<details><summary>Sources (%d)</summary><ul class="src">%s</ul></details>'
        '<label class="imp"><input type="checkbox"> implement?</label>'
        '<textarea class="note" rows="1" placeholder="Your note (optional)" aria-label="Note"></textarea>'
        '</article>'
        % (e(p["id"]), e(p["strength"]), e(text), e(p["principle"]), e(p["strength"]),
           p["n_independent_creators"], "" if p["n_independent_creators"] == 1 else "s",
           p["n_sources"], "" if p["n_sources"] == 1 else "s",
           e(EVIDENCE.get(p.get("best_evidence"), p.get("best_evidence") or "unrated")),
           det, fj, cf, len(p["sources"]), "".join(srcs))
    )


def render(lib):
    bks = [b for b in lib["buckets"] if b["principles"]]
    allp = [p for b in bks for p in b["principles"]]
    cnt = {k: sum(1 for p in allp if p["strength"] == k) for k in RANK}
    creators = {(s.get("author") or "unk:" + s["code"]) for p in allp for s in p["sources"]}
    nav = "".join('<a href="#%s">%s</a>' % (e(b["bucket"]), e(b["title"])) for b in bks)
    nav += '<a href="#conflicts">Conflicts</a>'
    secs = []
    for b in bks:
        ps = sorted(b["principles"], key=lambda p: (RANK.get(p["strength"], 3), -p["n_sources"],
                                                    -p["n_independent_creators"], p["id"]))
        secs.append('<section class="bk"><h2 id="%s">%s (%d)</h2>%s</section>'
                    % (e(b["bucket"]), e(b["title"]), len(ps), "".join(principle(p) for p in ps)))
    pid = {p["id"]: p for p in allp}
    conf = lib.get("conflicts") or []
    if not conf:
        seen = set()
        for p in allp:
            for c in p.get("conflicts_with", []):
                k = tuple(sorted((p["id"], c)))
                if k not in seen and c in pid:
                    seen.add(k)
                    conf.append({"a": k[0], "b": k[1], "reason": ""})
    cl = "".join(
        '<li><a href="#%s">%s</a> against <a href="#%s">%s</a>%s</li>'
        % (e(c["a"]), e(pid[c["a"]]["principle"] if c["a"] in pid else c["a"]),
           e(c["b"]), e(pid[c["b"]]["principle"] if c["b"] in pid else c["b"]),
           (". " + e(c["reason"])) if c.get("reason") else "")
        for c in conf)
    s = lib.get("sources", {})
    head = (
        "<h1>Advice library</h1>"
        '<p class="sub">%d principles from %d advice items across %d reels and %d creators, generated %s. '
        "%d strong, %d medium, %d weak.</p>"
        '<p class="sub">%s</p>'
        % (len(allp), s.get("items", 0), s.get("reels", 0), len(creators),
           e(lib.get("generated", "")), cnt["strong"], cnt["medium"], cnt["weak"], e(STRENGTH_RULE))
    )
    tools = (
        '<div class="tools"><input type="search" id="q" placeholder="Search principles, authors, quotes" aria-label="Search">'
        '<label><input type="radio" name="st" value="all" checked>all</label>'
        '<label><input type="radio" name="st" value="strong">strong</label>'
        '<label><input type="radio" name="st" value="medium">medium</label>'
        '<label><input type="radio" name="st" value="weak">weak</label>'
        '<label><input type="radio" name="st" value="ticked">implement only</label></div>'
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Advice library</title><style>%s</style></head><body><main>%s%s"
        "<nav>%s</nav>%s<p id=\"none\">No principles match.</p>"
        '<textarea id="out" readonly aria-label="Copied picks"></textarea>'
        '<section id="conflicts"><h2>Conflicts (%d)</h2><ul class="cf" style="padding-left:18px">%s</ul></section>'
        "</main><button id=\"copy\" type=\"button\">Copy my picks</button><script>%s</script></body></html>\n"
        % (CSS, head, tools, nav, "".join(secs), len(conf), cl, JS)
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reel-watcher advice", description=__doc__)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with open(a.inp, encoding="utf-8") as f:
        lib = json.load(f)
    out = render(lib)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(out)
    print("wrote %s (%d bytes)" % (a.out, len(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
