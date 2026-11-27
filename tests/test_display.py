from app import main


def test_no_display_on_linux_without_display_vars(monkeypatch):
    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert main.has_display() is False


def test_display_on_linux_with_display_var(monkeypatch):
    monkeypatch.setattr(main.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    assert main.has_display() is True


def test_windows_always_has_display(monkeypatch):
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert main.has_display() is True
