"""Shared visual constants and custom ttk styles (fixed 'litera' theme)."""
import ttkbootstrap as ttk

THEME = "litera"

FONT_FAMILY = "Segoe UI"
MONO_FONT = ("Consolas", 10)
TITLE_FONT = (FONT_FAMILY, 18, "bold")
SUBTITLE_FONT = (FONT_FAMILY, 10)
PILL_FONT = (FONT_FAMILY, 9, "bold")
HINT_FONT = (FONT_FAMILY, 9)
LABEL_BOLD = (FONT_FAMILY, 10, "bold")
MAC_FONT = ("Consolas", 24, "bold")
SEQ_FONT = (FONT_FAMILY, 26, "bold")
BANNER_FONT = (FONT_FAMILY, 13, "bold")
ICON_FONT = (FONT_FAMILY, 16, "bold")

PAD = 10
MUTED = "#868e96"


def configure_styles(style: ttk.Style) -> None:
    style.configure("Hint.TLabel", foreground=MUTED, font=HINT_FONT)
    style.configure("Mac.TLabel", font=MAC_FONT)
    style.configure("Seq.TLabel", font=SEQ_FONT, foreground=style.colors.primary)
    style.configure("Qr.Treeview", rowheight=74)
