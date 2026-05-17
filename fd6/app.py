import sys
from pathlib import Path
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication
from fd6.gui.main_window import MainWindow
from fd6.gui.splash import maybe_show_splash


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Forza Designer 6")
    app.setOrganizationName("FD6")
    # Window icon matches the current theme's badge (Default → Pink)
    from fd6.gui.brand_banner import badge_path
    from fd6.gui.themes import badge_filename_for_theme, saved_theme_name
    bp = badge_path(badge_filename_for_theme(saved_theme_name()))
    if bp:
        app.setWindowIcon(QIcon(str(bp)))

    # Apply persisted theme before constructing MainWindow so styling applies cleanly
    from fd6.gui.themes import apply_theme, saved_theme_name
    apply_theme(app, saved_theme_name())

    win = MainWindow()

    def show_main():
        win.show()

    # Show splash if SplashScreen.mp4 is present, then open main window when video ends or user clicks/keypress.
    # If no splash file, show main window immediately.
    splash = maybe_show_splash(show_main)
    if splash is None:
        # Already shown by callback above
        pass

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
