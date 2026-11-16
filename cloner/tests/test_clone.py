"""Clone logic against in-memory fake accounts (no network)."""
import pytest

from chomik_core import Agreement, ChomikError, Folder, FolderListing, RemoteFile
import chomik_cloner.clone as clone
from chomik_cloner.state import State

NOSLEEP = dict(sleep=lambda s: None)


class FakeDst:
    """Destination account. `files` maps folder path tuple -> list of names."""

    def __init__(self, username="dst", existing=None):
        self.username = username
        self.folders = {}
        self.files = {k: list(v) for k, v in (existing or {}).items()}

    def mkdir_p(self, parts):
        parts = tuple(parts)
        return self.folders.setdefault(parts, Folder(str(len(self.folders) + 1), parts[-1] if parts else "", parts))

    def list_folders(self, folder_id="0", parent_path=(), refresh=False):
        parent_path = tuple(parent_path)
        return [f for p, f in self.folders.items() if p[:-1] == parent_path and len(p) == len(parent_path) + 1]

    def arrive(self, folder, name):
        self.files.setdefault(folder.path, []).append(name)

    def lister(self, folder):
        return set(self.files.get(folder.path, []))

    def list_files(self, path, missing_ok=False):  # SOAP-style, for the default lister
        parts = tuple(p.replace("+", " ") for p in path.strip("/").split("/")[1:])
        return FolderListing(path, self.username, [rf(n) for n in self.files.get(parts, [])])


class FakeSrc:
    username = "src"


def rf(name, size=10, fid=None):
    return RemoteFile(fid or name, name, size, Agreement("own"), owner="src")


@pytest.fixture
def relays(monkeypatch):
    """Patch streaming: records calls and makes the file arrive (reliable, like the real upload)."""
    calls = []

    def fake_relay(src, f, dst, folder, **kw):
        if f.name == "boom.bin":
            raise ChomikError("upload exploded")
        calls.append((f.name, folder.path))
        dst.arrive(folder, f.name)
        return f.name

    monkeypatch.setattr(clone, "relay_file", fake_relay)
    return calls


class FakeWeb:
    """Server copy: `ghost` ids get an ok answer but never arrive; `late` ids arrive on the 2nd listing."""

    def __init__(self, dst, refuse=(), captcha_on=None, ghost=(), late=()):
        self.dst, self.refuse, self.captcha_on = dst, set(refuse), captcha_on
        self.ghost, self.late, self.copies, self.pending = set(ghost), set(late), [], []

    def copy_file(self, file_id, folder_id):
        from chomik_core.web import CaptchaRequired, WebCopyError

        if file_id == self.captcha_on:
            raise CaptchaRequired("captcha")
        if file_id in self.refuse:
            raise WebCopyError("copying disabled")
        self.copies.append((file_id, folder_id))
        folder = next(f for f in self.dst.folders.values() if f.id == folder_id)
        if file_id in self.late:
            self.pending.append((folder, file_id))
        elif file_id not in self.ghost:
            self.dst.arrive(folder, file_id)  # test files use id == name
        return "Plik został zachomikowany"

    def lister(self, folder):
        names = self.dst.lister(folder)
        for fol, name in self.pending:  # late files show up from the next listing on
            self.dst.arrive(fol, name)
        self.pending.clear()
        return names


def test_plan_counts_and_limits():
    source = [((), [rf("a"), rf("big", 999)], None), (("X",), [rf("b")], None), (("Y",), [], "denied")]
    plan = clone.build_plan(iter(source), max_size=100)
    assert [i.file.name for i in plan.items] == ["a", "b"]
    assert plan.folders == 3 and plan.list_errors == [("Y", "denied")]


def test_stream_run_copies_skips_existing_and_resumes(tmp_path, relays):
    dst = FakeDst(existing={("Root", "X"): ["b"]})
    plan = clone.build_plan(iter([((), [rf("a")], None), (("X",), [rf("b"), rf("boom.bin")], None)]))
    state = State(str(tmp_path / "s.sqlite"))
    res = clone.run(plan, FakeSrc(), dst, ("Root",), state, lister=dst.lister, **NOSLEEP)
    assert relays == [("a", ("Root",))]
    assert (res.copied, res.skipped, len(res.failed)) == (1, 1, 1)
    assert state.summary("dst") == {"done": 1, "skipped_exists": 1, "failed": 1}

    relays.clear()
    res2 = clone.run(plan, FakeSrc(), dst, ("Root",), state, lister=dst.lister, **NOSLEEP)
    assert relays == [] and res2.skipped == 2 and len(res2.failed) == 1


def test_default_soap_lister_still_works(tmp_path, relays):
    dst = FakeDst()
    plan = clone.build_plan(iter([((), [rf("a")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), **NOSLEEP)
    assert res.copied == 1


def test_refuses_same_account(tmp_path):
    with pytest.raises(ChomikError):
        clone.run(clone.Plan(), FakeSrc(), FakeDst(username="SRC"), (), State(str(tmp_path / "s")))


def test_server_copy_verified(tmp_path, relays):
    dst = FakeDst()
    web = FakeWeb(dst)
    plan = clone.build_plan(iter([((), [rf("a"), rf("b")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web),
                    lister=web.lister, **NOSLEEP)
    assert res.copied == 2 and not res.missing and relays == []


def test_late_arrival_counts_after_retry(tmp_path, relays):
    dst = FakeDst()
    web = FakeWeb(dst, late={"a"})
    plan = clone.build_plan(iter([((), [rf("a")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web),
                    lister=web.lister, **NOSLEEP)
    assert res.copied == 1 and not res.missing


def test_ghost_copy_is_missing_not_done_and_retried_next_run(tmp_path, relays):
    dst = FakeDst()
    web = FakeWeb(dst, ghost={"huge"})
    state = State(str(tmp_path / "s"))
    plan = clone.build_plan(iter([((), [rf("ok"), rf("huge", 11_000_000_000)], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), state, copier=clone.server_copier(web), lister=web.lister, **NOSLEEP)
    assert res.copied == 1 and res.missing == [("/huge", 11_000_000_000)]
    assert state.status("src", "huge", "dst") == "missing"
    web.ghost.clear()
    res2 = clone.run(plan, FakeSrc(), dst, (), state, copier=clone.server_copier(web), lister=web.lister, **NOSLEEP)
    assert res2.copied == 1 and res2.skipped == 1


def test_ghost_copy_falls_back_to_stream(tmp_path, relays):
    dst = FakeDst()
    web = FakeWeb(dst, ghost={"huge"})
    plan = clone.build_plan(iter([((), [rf("huge")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web),
                    lister=web.lister, fallback=clone.stream_copier, **NOSLEEP)
    assert res.copied == 1 and not res.missing and relays == [("huge", ())]


def test_refused_copy_is_failure(tmp_path, relays):
    dst = FakeDst()
    plan = clone.build_plan(iter([((), [rf("a")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")),
                    copier=clone.server_copier(FakeWeb(dst, refuse={"a"})), lister=dst.lister, **NOSLEEP)
    assert len(res.failed) == 1 and res.copied == 0


def test_captcha_stops_run_and_resume_continues(tmp_path, relays):
    dst = FakeDst()
    web = FakeWeb(dst, captcha_on="b")
    state = State(str(tmp_path / "s"))
    plan = clone.build_plan(iter([((), [rf("a"), rf("b"), rf("c")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), state, copier=clone.server_copier(web), lister=web.lister, pause=1, **NOSLEEP)
    assert res.stopped and res.copied == 1 and web.copies == [("a", "1")]
    web.captcha_on = None
    res2 = clone.run(plan, FakeSrc(), dst, (), state, copier=clone.server_copier(web), lister=web.lister, **NOSLEEP)
    assert res2.copied == 2 and res2.skipped == 1


def test_no_verify_trusts_copier(tmp_path, relays):
    dst = FakeDst()
    web = FakeWeb(dst, ghost={"a"})
    plan = clone.build_plan(iter([((), [rf("a")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web),
                    lister=web.lister, verify_delays=(), **NOSLEEP)
    assert res.copied == 1


def test_silent_failure_fixed_by_in_run_resend(tmp_path, relays):
    dst = FakeDst()

    class FlakyWeb(FakeWeb):
        def copy_file(self, file_id, folder_id):
            first = not any(c[0] == file_id for c in self.copies)
            if first:
                self.ghost.add(file_id)
            else:
                self.ghost.discard(file_id)
            return super().copy_file(file_id, folder_id)

    web = FlakyWeb(dst)
    plan = clone.build_plan(iter([((), [rf("dmg")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web),
                    lister=web.lister, **NOSLEEP)
    assert res.copied == 1 and not res.missing and len(web.copies) == 2


def test_expired_session_relogs_and_retries(tmp_path, relays):
    from chomik_core.web import WebCopyError

    dst = FakeDst()
    web = FakeWeb(dst)
    calls = {"n": 0, "relogin": 0}
    real = web.copy_file

    def flaky(fid, folder_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WebCopyError("/x: expected JSON, got HTTP 200 '<html>login'")
        return real(fid, folder_id)

    web.copy_file = flaky
    copier = clone.server_copier(web, relogin=lambda: calls.__setitem__("relogin", calls["relogin"] + 1))
    plan = clone.build_plan(iter([((), [rf("a")], None)]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=copier, lister=web.lister, **NOSLEEP)
    assert res.copied == 1 and calls["relogin"] == 1


def test_locked_source_folder_is_skipped_without_copy_attempts(tmp_path, relays):
    dst = FakeDst()
    web = FakeWeb(dst)
    state = State(str(tmp_path / "s"))
    source = [(("Open",), [rf("a")], None, "10"), (("Locked",), [rf("b"), rf("c")], None, "20")]
    plan = clone.build_plan(iter(source))
    res = clone.run(plan, FakeSrc(), dst, (), state, copier=clone.server_copier(web), lister=web.lister,
                    source_visible=lambda fid: fid != "20", **NOSLEEP)
    assert res.copied == 1 and res.locked == [("/Locked", 2)]
    assert [c[0] for c in web.copies] == ["a"]
    assert state.status("src", "b", "dst") == "locked"
    # once unlocked (password added), the rerun copies them
    res2 = clone.run(plan, FakeSrc(), dst, (), state, copier=clone.server_copier(web), lister=web.lister,
                     source_visible=lambda fid: True, **NOSLEEP)
    assert res2.copied == 2


def test_skip_dirs_drop_folder_and_everything_below():
    source = [((), [rf("a")], None), (("X", "@eaDir"), [rf("thumb")], None),
              (("X", "@eaDir", "v.mp4"), [rf("SYNOINDEX")], None), (("X",), [rf("v.mp4")], None)]
    plan = clone.build_plan(iter(source), skip_dirs=clone.DEFAULT_SKIP_DIRS)
    assert [i.file.name for i in plan.items] == ["a", "v.mp4"] and plan.folders == 2


class FolderWeb(FakeWeb):
    """Adds the site's folder copy: `src_folders` maps source folder id -> (name, file names)."""

    def __init__(self, dst, src_folders, drop=(), **kw):
        super().__init__(dst, **kw)
        self.src_folders, self.drop, self.folder_copies = src_folders, set(drop), []

    def copy_folder(self, owner, folder_id, parent_id):
        name, names = self.src_folders[folder_id]
        parent = next(f for f in [Folder("0", "", ()), *self.dst.folders.values()] if f.id == parent_id)
        self.folder_copies.append((folder_id, parent.path))
        new = self.dst.mkdir_p((*parent.path, name))
        for n in names:
            if n not in self.drop:  # some files never come along (seen live with some files)
                self.dst.arrive(new, n)
        return "Folder został zachomikowany"


def test_new_folder_is_copied_with_one_request(tmp_path, relays):
    dst = FakeDst()
    web = FolderWeb(dst, {"10": ("Books", ["a", "b", "c"])})
    plan = clone.build_plan(iter([(("Books",), [rf("a"), rf("b"), rf("c")], None, "10")]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web), lister=web.lister,
                    folder_copier=clone.server_folder_copier(web, "src"), **NOSLEEP)
    assert res.copied == 3 and web.folder_copies == [("10", ())] and web.copies == []


def test_files_missing_after_folder_copy_go_file_by_file(tmp_path, relays):
    dst = FakeDst()
    web = FolderWeb(dst, {"10": ("Books", ["a", "b"])}, drop={"b"})
    plan = clone.build_plan(iter([(("Books",), [rf("a"), rf("b")], None, "10")]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web), lister=web.lister,
                    folder_copier=clone.server_folder_copier(web, "src"), **NOSLEEP)
    assert res.copied == 2 and [c[0] for c in web.copies] == ["b"]


def test_existing_folder_is_never_folder_copied(tmp_path, relays):
    dst = FakeDst(existing={("Books",): ["a"]})
    dst.mkdir_p(("Books",))
    web = FolderWeb(dst, {"10": ("Books", ["a", "b"])})
    plan = clone.build_plan(iter([(("Books",), [rf("a"), rf("b")], None, "10")]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web), lister=web.lister,
                    folder_copier=clone.server_folder_copier(web, "src"), **NOSLEEP)
    assert web.folder_copies == [] and [c[0] for c in web.copies] == ["b"] and res.skipped == 1


def test_nested_new_folders_each_copied_into_their_parent(tmp_path, relays):
    dst = FakeDst()
    web = FolderWeb(dst, {"10": ("Top", ["a"]), "11": ("Sub", ["b"])})
    plan = clone.build_plan(iter([(("Top",), [rf("a")], None, "10"), (("Top", "Sub"), [rf("b")], None, "11")]))
    res = clone.run(plan, FakeSrc(), dst, ("Klon",), State(str(tmp_path / "s")), copier=clone.server_copier(web),
                    lister=web.lister, folder_copier=clone.server_folder_copier(web, "src"), **NOSLEEP)
    assert res.copied == 2 and web.folder_copies == [("10", ("Klon",)), ("11", ("Klon", "Top"))] and web.copies == []


def test_root_files_never_folder_copied(tmp_path, relays):
    dst = FakeDst()
    web = FolderWeb(dst, {})
    plan = clone.build_plan(iter([((), [rf("a")], None, "0")]))
    res = clone.run(plan, FakeSrc(), dst, (), State(str(tmp_path / "s")), copier=clone.server_copier(web), lister=web.lister,
                    folder_copier=clone.server_folder_copier(web, "src"), **NOSLEEP)
    assert res.copied == 1 and web.folder_copies == []


def test_account_items_adds_files_only_the_website_lists():
    from chomik_core.web import FolderLocked, WebFile

    class Src:
        username = "src"

        def walk(self):
            return iter([Folder("5", "Docs", ("Docs",)), Folder("6", "Safe", ("Safe",))])

        def list_files(self, path, missing_ok=False):
            return FolderListing(path, "src", [rf("X(2).pdf", fid="3")])

    def web_files(fid):
        if fid == "6":
            raise FolderLocked("locked")
        return [WebFile("1", "X.pdf", "1,0 MB"), WebFile("3", "X(2).pdf", "1,0 MB")]

    got = {rel: [f.name for f in files] for rel, files, err, fid in clone.account_items(Src(), web_files=web_files)}
    assert got[("Docs",)] == ["X(2).pdf", "X.pdf"] and got[("Safe",)] == ["X(2).pdf"]
    extra = next(f for rel, files, *_ in clone.account_items(Src(), web_files=web_files) for f in files if f.id == "1")
    assert extra.size == 1024**2 and extra.owner == "src"
