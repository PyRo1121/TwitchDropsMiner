"""The Drop Deck visual system.

This is intentionally not a card-based SaaS dashboard. The dark theme uses a
cool ink canvas, editorial type hierarchy, ruled sections, and a signal-mark
accent system. Components should create structure with alignment and
whitespace before adding a surface or decoration.
"""
from __future__ import annotations

from dataclasses import dataclass



@dataclass(frozen=True)
class Palette:
    bg: str
    surface2: str
    surface3: str
    border: str
    text: str
    muted: str
    subtle: str
    accent: str
    accent2: str
    green: str
    amber: str
    error: str
    info: str
    idle: str


# The ultraviolet/aqua signal palette gives the cockpit its own identity
# without borrowing Twitch's purple as the primary brand color.
DARK = Palette(
    bg="#0b0d14",
    surface2="#171b2a",
    surface3="#20263a",
    border="rgba(232,236,255,0.13)",
    text="#eef1f8",
    muted="#a1a9bd",
    subtle="#69728a",
    accent="#b69cff",
    accent2="#6f5bff",
    green="#5fe1d3",
    amber="#ffb86b",
    error="#ff6f91",
    info="#8fc6ff",
    idle="#a1a9bd",
)

LIGHT = Palette(
    bg="#f4f5f9",
    surface2="#eef0f7",
    surface3="#e1e5f0",
    border="rgba(22,27,44,0.16)",
    text="#171a26",
    muted="#596178",
    subtle="#737d95",
    accent="#6653c7",
    accent2="#4c3aa8",
    green="#1f988f",
    amber="#ad6b12",
    error="#c14468",
    info="#2d6f9d",
    idle="#596178",
)


@dataclass(frozen=True)
class Theme:
    dark: bool
    p: Palette
    base_pt: int = 10

    def _sheet(self) -> str:
        p = self.p
        accent_rgba = "rgba(182,156,255,0.13)" if self.dark else "rgba(102,83,199,0.12)"
        focus = p.accent
        return f"""
/* ---------- foundation: continuous ink canvas ---------- */
QWidget {{
    background: {p.bg}; color: {p.text};
    font-family: "Noto Sans", "Liberation Sans", "Segoe UI", sans-serif;
}}
QMainWindow, QDialog {{ background: {p.bg}; }}
QLabel {{ background: transparent; color: {p.text}; }}
QLabel#muted {{ color: {p.muted}; }}
QLabel#subtle {{ color: {p.subtle}; }}
QLabel#eyebrow, QLabel#pageKicker {{
    color: {p.subtle}; font-family: "JetBrains Mono", "Liberation Mono", monospace;
    font-size: 8pt; font-weight: 700; letter-spacing: 1.4px;
}}
QLabel#h1 {{
    color: {p.text}; font-family: "Liberation Serif", "Georgia", serif;
    font-size: 25pt; font-weight: 700; letter-spacing: -0.7px;
}}
QLabel#h2 {{
    font-family: "Liberation Serif", "Georgia", serif;
    font-size: 16pt; font-weight: 700; color: {p.text};
}}
QLabel#pageTitle {{ font-size: 11pt; font-weight: 700; color: {p.text}; }}
QLabel#statValue {{
    font-family: "JetBrains Mono", "Liberation Mono", monospace;
    font-size: 20pt; font-weight: 700; letter-spacing: -0.6px;
}}
QLabel#heroGame {{
    font-family: "Liberation Serif", "Georgia", serif;
    font-size: 30pt; font-weight: 700; letter-spacing: -1px;
}}
QLabel#heroCaption {{ font-size: 10pt; color: {p.muted}; }}
QLabel#heroPercent {{
    color: {p.text}; font-family: "JetBrains Mono", "Liberation Mono", monospace;
    font-size: 9pt; font-weight: 700;
}}
QLabel#heroChannel {{
    color: {p.green}; font-family: "JetBrains Mono", "Liberation Mono", monospace;
    font-size: 8pt; font-weight: 700; letter-spacing: 0.8px;
}}
QLabel#heroIntel {{
    color: {p.info}; font-family: "JetBrains Mono", "Liberation Mono", monospace;
    font-size: 8pt; font-weight: 700; letter-spacing: 0.5px;
}}
QLabel#gameArt {{
    background: {p.surface3}; color: {p.subtle}; border: 1px solid {p.border};
    font-family: "JetBrains Mono", "Liberation Mono", monospace; font-size: 8pt;
    letter-spacing: 1px;
}}

/* ---------- shell: a ruled navigation spine ---------- */
QFrame#sidebar {{
    background: {p.bg}; border-right: 1px solid {p.border};
}}
QFrame#brand {{ background: transparent; }}
QLabel#brandName {{
    font-family: "JetBrains Mono", "Liberation Mono", monospace;
    font-size: 11pt; font-weight: 800; letter-spacing: 1px;
}}
QLabel#brandSub {{
    color: {p.subtle}; font-family: "JetBrains Mono", "Liberation Mono", monospace;
    font-size: 7pt; font-weight: 700; letter-spacing: 1.6px;
}}
QLabel#brandMark {{
    background: #eef1f8; border: 1px solid {p.border}; color: {p.bg};
    border-radius: 14px; padding: 4px;
}}
QPushButton#nav {{
    background: transparent; border: none; border-left: 3px solid transparent;
    border-radius: 0; color: {p.muted}; text-align: left;
    padding: 10px 9px; min-height: 38px; font-size: 9pt; font-weight: 650;
}}
QPushButton#nav:hover {{ color: {p.text}; background: {accent_rgba}; }}
QPushButton#nav:checked {{
    color: {p.accent}; background: transparent; border-left-color: {p.accent};
}}
QFrame#topbar {{ background: {p.bg}; border-bottom: 1px solid {p.border}; }}
QLabel#topbarContext {{ color: {p.muted}; font-size: 9pt; }}
QLabel#topbarSlash {{ color: {p.subtle}; font-size: 10pt; }}
QLineEdit#command {{
    background: transparent; border: none; border-bottom: 1px solid {p.border};
    border-radius: 0; padding: 8px 2px; color: {p.text};
    selection-background-color: {focus};
}}
QLineEdit#command:hover, QLineEdit#command:focus {{ border-bottom-color: {p.accent}; }}
QFrame#statusChip {{
    background: transparent; border: 1px solid {p.border};
    border-radius: 0; padding: 4px 10px;
}}

/* ---------- surfaces: rules and one focal hero, never a card pile ---------- */
QFrame#card, QFrame#panel {{
    background: transparent; border: none; border-radius: 0;
}}
QFrame#selectedCard {{
    background: {accent_rgba}; border: none; border-left: 2px solid {p.accent};
    border-top: 1px solid {p.accent}; border-bottom: 1px solid {p.accent}; border-radius: 0;
}}
QFrame#surface2 {{
    background: {p.surface2}; border: none; border-left: 3px solid {p.accent};
    border-top: 1px solid {p.border}; border-bottom: 1px solid {p.border}; border-radius: 0;
}}
QFrame#heroCard {{
    background: {p.surface2}; border: 1px solid {p.border}; border-left: 3px solid {p.accent};
    border-radius: 2px;
}}
QFrame#statTile {{
    background: transparent; border: none; border-left: 1px solid {p.border}; border-radius: 0;
}}
QFrame#statTile:hover {{ background: {accent_rgba}; }}
QFrame#quickCard {{
    background: transparent; border: none; border-top: 1px solid {p.border};
    border-bottom: 1px solid {p.border}; border-radius: 0;
}}
QFrame#loginPanel {{
    background: {p.surface2}; border: none; border-left: 3px solid {p.accent};
    border-top: 1px solid {p.border}; border-bottom: 1px solid {p.border}; border-radius: 0;
}}
QLabel#loginIcon {{ background: {accent_rgba}; border-radius: 0; padding: 6px; }}
QLabel#statIcon {{ background: {accent_rgba}; border-radius: 0; padding: 6px; }}
QFrame#activityShell, QWidget#activityShell {{
    background: transparent; border: none; border-top: 1px solid {p.border};
    border-bottom: 1px solid {p.border}; border-radius: 0;
}}
QFrame#channelRow, QFrame#campaignRow {{
    background: transparent; border: none; border-bottom: 1px solid {p.border}; border-radius: 0;
}}
QFrame#channelRow:hover, QFrame#campaignRow:hover {{ background: {accent_rgba}; }}
QFrame#selectedChannelRow {{
    background: {accent_rgba}; border: none; border-left: 3px solid {p.accent};
    border-bottom: 1px solid {p.accent}; border-radius: 0;
}}
QFrame#dropRow {{
    background: transparent; border: none; border-left: 2px solid {p.accent};
    border-bottom: 1px solid {p.border}; border-radius: 0;
}}
QFrame#metricStrip {{ background: transparent; border-top: 1px solid {p.border}; }}
QFrame#statusRibbon {{
    background: transparent; border-left: 2px solid {p.accent};
    border-bottom: 1px solid {p.border};
}}
QFrame#metricRail {{
    background: transparent; border-top: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
}}
QFrame#metricRail > QWidget#metricTile {{
    border-right: 1px solid {p.border}; padding: 0 9px;
}}
QFrame#metricRail > QWidget#metricTile[lastMetric="true"] {{ border-right: none; }}

/* ---------- controls: square, legible, quiet ---------- */
QLineEdit, QComboBox, QSpinBox {{
    min-height: 20px; background: {p.surface2}; color: {p.text};
    border: 1px solid {p.border}; border-radius: 0; padding: 8px 11px;
    selection-background-color: {focus};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView, QCompleter QAbstractItemView {{
    background: {p.surface3}; color: {p.text}; border: 1px solid {p.border};
    selection-background-color: {accent_rgba}; selection-color: {p.text};
}}
QPushButton {{
    background: {p.surface2}; color: {p.text}; border: 1px solid {p.border};
    border-radius: 0; padding: 8px 14px; font-weight: 650;
}}
QPushButton:hover {{ background: {accent_rgba}; border-color: {p.accent}; }}
QPushButton:focus {{ border: 2px solid {focus}; padding: 7px 13px; }}
QPushButton:pressed {{ background: {p.surface3}; }}
QPushButton:disabled {{ color: {p.subtle}; background: transparent; }}
QPushButton#primary {{ background: {p.accent}; color: {p.bg}; border: 1px solid {p.accent}; }}
QPushButton#primary:hover {{ background: {p.text}; color: {p.bg}; border-color: {p.text}; }}
QPushButton#primary:disabled {{ background: {p.surface2}; color: {p.subtle}; border-color: {p.border}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {p.muted}; }}
QPushButton#ghost:hover {{ color: {p.accent}; background: transparent; }}
QPushButton#iconButton {{ background: transparent; border: 1px solid transparent; border-radius: 0; padding: 7px; }}
QPushButton#iconButton:hover {{ background: {accent_rgba}; border-color: {p.accent}; }}
QPushButton#heroLink, QPushButton#rowAction {{
    background: transparent; color: {p.muted}; border: 1px solid {p.border};
    border-radius: 0; padding: 4px 7px; font-size: 8pt; font-weight: 700;
    letter-spacing: 0.7px;
}}
QPushButton#heroLink:hover, QPushButton#rowAction:hover {{
    color: {p.accent}; background: {accent_rgba}; border-color: {p.accent};
}}
QPushButton#heroLink:disabled, QPushButton#rowAction:disabled {{
    color: {p.subtle}; border-color: transparent;
}}
QLineEdit#searchField {{
    background: {p.surface2}; border: 1px solid {p.border}; border-radius: 0;
    padding: 9px 11px; color: {p.text};
}}
QLineEdit#searchField:focus {{ border-color: {p.accent}; }}
QPushButton#danger {{ background: transparent; color: {p.error}; border: 1px solid {p.error}; border-radius: 0; }}
QCheckBox#filterChip {{
    background: transparent; color: {p.muted}; border: 1px solid {p.border};
    border-radius: 0; padding: 7px 12px; spacing: 7px; font-weight: 650;
}}
QCheckBox#filterChip:hover {{ color: {p.text}; border-color: {p.accent}; }}
QCheckBox#filterChip:checked {{ background: {accent_rgba}; color: {p.text}; border-color: {p.accent}; }}
QCheckBox#filterChip::indicator {{
    width: 7px; height: 7px; border: none; border-radius: 0; background: {p.surface3};
}}
QCheckBox#filterChip::indicator:checked {{ background: {p.accent}; }}
QCheckBox, QRadioButton {{ background: transparent; color: {p.text}; spacing: 8px; }}
QCheckBox:focus, QRadioButton:focus {{ color: {focus}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1px solid {p.border}; border-radius: 0; background: {p.surface2};
}}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QProgressBar {{
    background: {p.surface3}; border: none; border-radius: 0;
    text-align: center; color: transparent; height: 3px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p.accent2}, stop:1 {p.accent});
    border-radius: 0;
}}

/* ---------- lists, logs, and focus ---------- */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.subtle}; border-radius: 0; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 6px; }}
QScrollBar::handle:horizontal {{ background: {p.subtle}; border-radius: 0; min-width: 30px; }}
QToolTip {{ background: {p.surface3}; color: {p.text}; border: 1px solid {p.border}; padding: 5px 8px; }}
QTabWidget::pane {{ border: none; background: {p.bg}; }}
QTabBar::tab {{ background: transparent; color: {p.muted}; padding: 8px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
QTableWidget, QListView, QTreeView, QListWidget {{
    background: transparent; color: {p.text}; alternate-background-color: {p.surface2};
    border: 1px solid {p.border}; border-radius: 0;
}}
QTableWidget:focus, QListView:focus, QTreeView:focus, QListWidget:focus {{
    border: 2px solid {focus};
}}
QPlainTextEdit, QTextEdit {{
    background: {p.surface2}; color: {p.muted}; border: 1px solid {p.border};
    border-radius: 0; padding: 10px; selection-background-color: {focus};
    font-family: "JetBrains Mono", "Liberation Mono", monospace;
}}
QHeaderView::section {{ background: {p.surface2}; color: {p.muted}; border: none; padding: 7px 10px; font-weight: 650; }}
QListWidget::item, QTreeWidget::item {{ padding: 7px 10px; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {accent_rgba}; color: {p.text}; border-radius: 0; }}
QSplitter::handle {{ background: {p.border}; }}
"""

    @property
    def qss(self) -> str:
        return self._sheet()


def apply_theme(app, theme: Theme) -> None:
    """Apply the semantic theme globally to a QApplication."""
    app.setProperty("tdmDarkTheme", theme.dark)
    app.setStyleSheet(theme.qss)
    font = app.font()
    font.setPointSize(theme.base_pt)
    app.setFont(font)


def make_theme(dark: bool) -> Theme:
    return Theme(dark=dark, p=DARK if dark else LIGHT)
