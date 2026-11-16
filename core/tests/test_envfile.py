from chomik_core.envfile import load_env_file


def test_load_env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('# c\nCHX_A=1\nexport CHX_B="two words"\nCHX_EMPTY=\nCHX_KEEP=new\n')
    monkeypatch.setenv("CHX_KEEP", "old")
    for k in ("CHX_A", "CHX_B", "CHX_EMPTY"):
        monkeypatch.delenv(k, raising=False)
    assert load_env_file(str(p)) == ["CHX_A", "CHX_B"]
    import os

    assert os.environ["CHX_B"] == "two words"
    assert os.environ["CHX_KEEP"] == "old"
    assert "CHX_EMPTY" not in os.environ


def test_missing_file_is_fine(tmp_path):
    assert load_env_file(str(tmp_path / "nope")) == []
