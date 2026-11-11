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
STAT_FONT = (FONT_FAMILY, 14, "bold")

PAD = 10
MUTED = "#868e96"
CARD_BG = "#f1f4f8"


def configure_styles(style: ttk.Style) -> None:
    style.configure("Hint.TLabel", foreground=MUTED, font=HINT_FONT)
    style.configure("Mac.TLabel", font=MAC_FONT)
    style.configure("Seq.TLabel", font=SEQ_FONT, foreground=style.colors.primary)
    style.configure("Qr.Treeview", rowheight=74)
    # MQTT tab: summary cards + roomier tables
    style.configure("Stat.TFrame", background=CARD_BG)
    style.configure("StatHint.TLabel", background=CARD_BG, foreground=MUTED, font=HINT_FONT)
    style.configure("StatValue.TLabel", background=CARD_BG, foreground=style.colors.fg, font=STAT_FONT)
    style.configure("StatValueSm.TLabel", background=CARD_BG, foreground=style.colors.fg, font=(FONT_FAMILY, 11, "bold"))
    style.configure("Data.Treeview", rowheight=26, font=(FONT_FAMILY, 10))
    style.configure("Data.Treeview.Heading", font=LABEL_BOLD, padding=(6, 5))
