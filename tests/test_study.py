"""study: input parsing, prioritization, Apify mapping, dedupe, giveaway detection (no models, no network)."""
import json

from reel_watcher import study as rs


def test_parse_and_prioritize(tmp_path):
    f = tmp_path / "in.txt"
    f.write_text("https://www.instagram.com/reel/A1/\tHooks\n"
                 "https://www.instagram.com/p/B2/\tInspo|Hooks\n"
                 "https://www.instagram.com/reel/C3/\tWorkflow upgrades\n"
                 "https://www.instagram.com/reel/A1/\tdup\n"
                 "https://example.com/x\tbad\n")
    items = rs.parse_input(f)
    assert [i["code"] for i in items] == ["A1", "B2", "C3"]
    assert items[1]["kind"] == "carousel" and items[1]["collections"] == ["Inspo", "Hooks"]
    pr = rs.prioritize(items, ["workflow", "inspo"], False)
    assert [i["code"] for i in pr] == ["C3", "B2", "A1"]
    assert [i["code"] for i in rs.prioritize(items, ["inspo"], True)] == ["B2"]


def test_comment_words():
    assert rs.comment_words('Comment "GUIDE" below', "comment the word PROMPT") == ["GUIDE", "PROMPT"]
    assert rs.comment_words("leave a comment below") == []


def test_apify_batch_mapping(tmp_path, monkeypatch):
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    img = tmp_path / "i.jpg"
    img.write_bytes(b"y")
    data = [
        {"shortCode": "A1", "url": "u", "caption": "cap", "ownerUsername": "own", "likesCount": -1, "videoViewCount": 50,
         "commentsCount": 2, "timestamp": "2026-05-01T10:00:00.000Z", "videoUrl": f"file://{vid}"},
        {"shortCode": "B2", "type": "Sidecar", "childPosts": [{"displayUrl": f"file://{img}"}, {"displayUrl": f"file://{img}"}]},
    ]
    calls = []

    def fake(method, url, body=None, timeout=120, token=None):
        assert token == "TOKEN" and "TOKEN" not in url
        calls.append(url)
        if method == "POST":
            assert body["directUrls"] == ["https://www.instagram.com/reel/A1/", "https://www.instagram.com/p/B2/", "https://www.instagram.com/reel/C3/"]
            assert "maxTotalChargeUsd=2.0" in url
            return {"data": {"id": "r1", "status": "RUNNING"}}
        if "actor-runs" in url:
            return {"data": {"id": "r1", "status": "SUCCEEDED", "defaultDatasetId": "d1", "usageTotalUsd": 0.0123}}
        return data
    monkeypatch.setattr(rs, "http_json", fake)
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    batch = [{"url": f"https://www.instagram.com/{k}/{c}/", "code": c, "collections": [], "kind": "reel"}
             for k, c in (("reel", "A1"), ("p", "B2"), ("reel", "C3"))]
    units = rs.prepare_apify_batch(batch, tmp_path, tmp_path / "w", "TOKEN", 2.0)
    by = {u["item"]["code"]: u for u in units}
    assert by["A1"]["item"]["kind"] == "reel" and by["A1"]["meta"]["likes"] is None and by["A1"]["meta"]["views"] == 50
    assert by["A1"]["meta"]["post_date"] == "2026-05-01" and by["A1"]["media"][0].read_bytes() == b"x"
    assert by["B2"]["item"]["kind"] == "carousel" and len(by["B2"]["media"]) == 2
    assert by["C3"]["error"].startswith("apify_no_result")
    cost = json.loads((tmp_path / "apify_cost.jsonl").read_text())
    assert cost["usd"] == 0.0123 and "TOKEN" not in json.dumps(cost)


def test_giveaway_comment_dm_link():
    g = rs.parse_giveaway([("transcript", "Comment CLAUDE below and I'll send you my full config pack."), ("caption", "")],
                          None, {"offered": True, "asset_type": "repo", "what_it_is": "my Claude Code config"})
    assert g["offered"] and g["keyword"] == "CLAUDE" and g["channel"] == "comment"
    assert "config" in g["what_they_get"] and g["evidence"][0]["where"] == "transcript"
    d = rs.parse_giveaway([("caption", "DM me GUIDE to get the free guide. Link in bio too.")])
    assert d["channel"] == "DM" and d["keyword"] == "GUIDE" and "guide" in d["what_they_get"].lower()
    lk = rs.parse_giveaway([("on-screen@3.0s", "link in bio for the template")])
    assert lk["offered"] and lk["channel"] == "link in bio" and lk["keyword"] == ""
    assert rs.parse_giveaway([("transcript", "I will message you back later, dm me about it")])["keyword"] == ""
    none = rs.parse_giveaway([("transcript", "just a normal reel")], {"comment_word": ""}, {"offered": False})
    assert none["offered"] is False and none["evidence"] == []


def test_build_frames_and_archive(tmp_path):
    fr = rs.build_frames([{"t": 1.0, "visual": {"graphic_type": "UI demo"}, "shot_type": "screen recording"}], {1.0: "frames/f_000100.jpg"})
    assert fr == [{"t": 1.0, "file": "frames/f_000100.jpg", "visual": {"graphic_type": "UI demo"}, "shot_type": "screen recording"}]
    work = tmp_path / "w"
    (work / "_arch").mkdir(parents=True)
    (work / "_arch" / "f_000100.jpg").write_bytes(b"x")
    (work / "a.mp4").write_bytes(b"v")
    (work / "a.info.json").write_text("{}")
    d = rs.archive_commit(tmp_path / "arch", {"code": "A1", "url": "u", "kind": "reel", "collections": []}, {}, {"frames": fr}, work, [work / "a.mp4"])
    m = json.loads((d / "manifest.json").read_text())
    assert {f["kind"] for f in m["files"]} == {"video", "info", "meta", "frame"} and (d / "frames/f_000100.jpg").exists()


def test_dedupe_frames_hash_and_text():
    a, b = 0, 0b111  # distance 3 from a
    far = (1 << 40) - 1  # distance 40
    # frame 1 near frame 0 and same text -> inherits; frame 2 near but text changed -> kept; frame 3 far -> kept
    uniq, rep = rs.dedupe_frames([a, b, b, far], ["Hello world", "hello  world", "Subscribe now", ""], thr=9, cap=9)
    assert uniq == [0, 2, 3] and rep == [0, 0, 2, 3]


def test_dedupe_cap_thins_and_inherits():
    hashes = [(1 << (i * 5)) - 1 if i else 0 for i in range(12)]  # all mutually far
    uniq, rep = rs.dedupe_frames(hashes, [""] * 12, thr=2, cap=9)
    assert len(uniq) == 9 and uniq[0] == 0 and all(r in uniq for r in rep) and all(rep[u] == u for u in uniq)
    # texts differ: a merged frame never inherits a different-text survivor when avoidable
    uniq2, rep2 = rs.dedupe_frames([0, 1, 1 << 30, (1 << 30) | 1], ["a", "a", "b", "b"], thr=0, cap=2)
    assert uniq2 == [0, 2] and rep2 == [0, 0, 2, 2]


def test_map_tiles_by_index_and_inherit():
    res = {"tiles": [{"i": 2, "graphic_type": "screenshot", "what_it_shows": "terminal", "worth_studying": True, "why": "clean"},
                     {"i": 1, "graphic_type": "talking head", "font_feel": {"family_guess": "Impact"}, "editing_technique": "text pop",
                      "worth_studying": {"worth": False, "why": "plain"}},
                     {"i": 7, "graphic_type": "other"}]}
    m = rs.map_tiles(res, 2)
    assert set(m) == {0, 1}
    assert m[0]["graphic_type"] == "talking head" and m[0]["editing_technique"] == ["text pop"] and m[0]["worth_studying"] == {"worth": False, "why": "plain"}
    assert m[1]["worth_studying"] == {"worth": True, "why": "clean"} and m[1]["font_feel"] is None
    assert rs.map_tiles({"nope": 1}, 3) == {}


def test_turbo_visuals_duplicates_inherit(tmp_path, monkeypatch):
    from PIL import Image
    paths = []
    for n, shade in enumerate((10, 12, 200)):  # frames 0,1 near-identical flat; frame 2 differs
        p = tmp_path / f"{n}.jpg"
        im = Image.new("RGB", (90, 160), (shade,) * 3)
        for x in range(0, 90, 2):
            im.putpixel((x, 5 + n * 40), (255 - shade,) * 3)
        im.save(p)
        paths.append(p)
    monkeypatch.setattr(rs, "ocr_image", lambda p: f"text {p.stem}" if p.stem == "2" else "same")

    class V:
        def ask(self, prompt, image, max_tokens=0):
            return {"tiles": [{"i": 1, "graphic_type": "b-roll"}, {"i": 2, "graphic_type": "text card"}], "reel": {"format": "montage"}}
    recs, ov, t = rs.turbo_visuals(V(), paths, [0.0, 0.5, 1.0], ["0s", "0.5s", "1s"], tmp_path, "reel", "timestamp")
    assert t["unique_frames"] == 2 and ov == {"format": "montage"}
    assert [r["visual"]["graphic_type"] for r in recs] == ["b-roll", "b-roll", "text card"]
    assert [r["visual"]["on_screen_text"] for r in recs] == ["same", "same", "text 2"]
    assert (tmp_path / "_sheet.jpg").exists()
