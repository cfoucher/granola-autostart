"""Windows notifications for problems the user should know about.

Notifications appear under the app's own name and icon. The installer's Start menu shortcut carries
the app ID; register_app_id() also records it in the registry so running from source works too.
Button clicks are handled by the running watcher. If toasts are unavailable, a tray balloon is used.
"""

from __future__ import annotations

import logging
import winreg
from pathlib import Path
from typing import Callable

from .status import Problem

log = logging.getLogger("granola_autostart")
APP_ID = "GranolaAutostart"
APP_NAME = "Granola Autostart"


def register_app_id(icon: Path) -> None:
    key_path = rf"Software\Classes\AppUserModelId\{APP_ID}"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))


class Notifier:
    """``on_action(action_id, problem)`` runs on a Windows thread when a button or the body is clicked."""

    def __init__(
        self, on_action: Callable[[str, Problem], None], fallback: Callable[[str, str], None], logo: Path | None = None
    ) -> None:
        self._on_action = on_action
        self._fallback = fallback
        self._logo = logo
        self._toaster = None
        try:
            from windows_toasts import InteractableWindowsToaster

            self._toaster = InteractableWindowsToaster(APP_NAME, notifierAUMID=APP_ID)
        except Exception as error:
            log.warning("Windows notifications unavailable (%s); using tray balloons", type(error).__name__)

    def show(self, problem: Problem) -> None:
        log.warning("%s: %s", problem.title, problem.detail)
        if self._toaster is None:
            self._fallback(problem.title, problem.detail)
            return
        try:
            from windows_toasts import Toast, ToastButton, ToastDisplayImage, ToastImagePosition

            # A click on the body arrives with no arguments, which _activated treats as "open".
            toast = Toast(text_fields=[problem.title, problem.detail])
            if self._logo is not None and self._logo.exists():
                # The app's icon inside the notification, whether or not Windows shows it in the header.
                toast.AddImage(ToastDisplayImage.fromPath(self._logo, position=ToastImagePosition.AppLogo))
            for label, action in problem.actions:
                toast.AddAction(ToastButton(label, action))
            toast.on_activated = lambda event: self._activated(event, problem)
            self._toaster.show_toast(toast)
        except Exception as error:
            log.warning("notification failed (%s); using a tray balloon", type(error).__name__)
            self._fallback(problem.title, problem.detail)

    def _activated(self, event, problem: Problem) -> None:
        try:
            self._on_action(getattr(event, "arguments", None) or "open", problem)
        except Exception:
            log.exception("notification action failed")
