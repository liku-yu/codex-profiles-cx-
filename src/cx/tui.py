"""Interactive TUI for codex-profiles, built with Textual."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from cx import apply as apply_mod
from cx import catalog as catalog_mod
from cx import sessions as sessions_mod
from cx import state
from cx import usage as usage_mod
from cx.codex import (
    codex_home,
    codex_version,
    find_codex,
    list_profile_names,
    official_models,
    probe_profile,
    profile_path,
    restart_app_server_daemon,
)
from cx.detect import DetectedModel, detect_models
from cx.presets import ProviderPreset, community_presets, find_preset, official_preset
from cx.profiles import (
    ProfileError,
    build_profile_document,
    delete_profile,
    load_dict,
    load_text,
    read_base_config,
    summarize,
    write_profile,
)
from cx.redact import redact_text, redact_toml
from cx.schema import provider_reference_issues, validate_document
from cx.theme import CODEX_THEME

DEFAULT_THEME = "codex"

# The scalar keys the form manages on a profile.
_MANAGED_KEYS = ("model", "model_reasoning_effort", "model_provider", "model_catalog_json")
_FORM_FIELDS = (
    "name",
    "provider_id",
    "base_url",
    "api_key",
    "model_catalog_url",
    "model",
    "effort",
)


# --------------------------------------------------------------------------
# model picker
# --------------------------------------------------------------------------
class ModelPickerScreen(ModalScreen[Optional[DetectedModel]]):
    """Show detected models together with their reasoning levels."""

    CSS = """
    ModelPickerScreen { align: center middle; }
    #box { width: 90; height: 80%; padding: 1 2; background: $panel; border: round $primary; }
    #box Label.title { text-style: bold; padding-bottom: 1; }
    #models { height: 1fr; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, models: list[DetectedModel]) -> None:
        super().__init__()
        self.models = models

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"检测到 {len(self.models)} 个模型，请选择", classes="title")
            yield DataTable(id="models", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#models", DataTable)
        table.add_columns("模型", "推理等级", "默认", "来源")
        for index, model in enumerate(self.models):
            table.add_row(
                model.display_name or model.slug,
                ", ".join(model.efforts) or "-",
                model.default_effort or "-",
                model.source,
                key=str(index),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value is not None:
            self.dismiss(self.models[int(str(event.row_key.value))])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# provider picker
# --------------------------------------------------------------------------
CUSTOM_PRESET = ProviderPreset(id="__custom__", name="自定义 / 手动", category="自定义")


def _preset_models(preset: ProviderPreset) -> list[DetectedModel]:
    return [
        DetectedModel(
            slug=model.slug,
            display_name=model.display_name or model.slug,
            efforts=list(model.levels),
            default_effort=model.default,
            source="preset",
        )
        for model in preset.models
    ]


class ProviderPickerScreen(ModalScreen[Optional[ProviderPreset]]):
    """Pick a bundled provider preset, official OpenAI, or fully custom."""

    CSS = """
    ProviderPickerScreen { align: center middle; }
    #box { width: 96; height: 80%; padding: 1 2; background: $panel; border: round $primary; }
    #box Label.title { text-style: bold; padding-bottom: 1; }
    #providers { height: 1fr; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self) -> None:
        super().__init__()
        self._presets: list[ProviderPreset] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("选择供应商", classes="title")
            yield DataTable(id="providers", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#providers", DataTable)
        table.add_columns("分类", "供应商", "base_url")
        self._presets = [official_preset(), *community_presets(), CUSTOM_PRESET]
        for index, preset in enumerate(self._presets):
            table.add_row(
                preset.category or "-",
                preset.name,
                preset.base_url or "（内置 / 手动）",
                key=str(index),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value is not None:
            self.dismiss(self._presets[int(str(event.row_key.value))])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# unified create / edit form
# --------------------------------------------------------------------------
class ProfileFormScreen(ModalScreen[Optional[dict[str, str]]]):
    """One form to edit both the provider and the model of a profile.

    Used for creating (``existing=None``) and for editing an existing profile
    (``existing={"name", "data"}``).
    """

    CSS = """
    ProfileFormScreen { align: center middle; }
    #form { width: 78; height: auto; padding: 1 2; background: $panel; border: round $primary; }
    #form Label.title { text-style: bold; padding-bottom: 1; }
    #hint { height: auto; color: $text-muted; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(
        self,
        preset: Optional[ProviderPreset] = None,
        existing: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self.preset = preset
        self.existing = existing
        self._efforts: list[str] = []
        self._known_models: list[DetectedModel] = []

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        title = "编辑 Profile" if self.existing else "新建 Profile"
        with Vertical(id="form"):
            yield Label(title, classes="title", id="title")
            yield Input(placeholder="名称（字母 / 数字 / _ / -）", id="name")
            yield Input(placeholder="供应商 id（可选，默认同名称）", id="provider_id")
            yield Input(placeholder="base_url（可选，留空=官方 openai）", id="base_url")
            yield Input(
                placeholder="API Key（直接写入配置，无需环境变量）", password=True, id="api_key"
            )
            yield Input(placeholder="model_catalog_url（可选，Codex 模型目录）", id="model_catalog_url")
            yield Label("", id="hint")
            yield Input(placeholder="模型，例如 gpt-5.1-codex", id="model")
            yield Input(placeholder="推理强度，例如 high（可选）", id="effort")
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")
                yield Button("选择供应商", id="provider")
                yield Button("检测模型", id="detect")
                yield Button("创建", id="save", variant="primary")

    def on_mount(self) -> None:
        if self.existing:
            self.query_one("#name", Input).disabled = True
            self.query_one("#save", Button).label = "保存"
            self._fill_existing(self.existing.get("data", {}))
        elif self.preset is not None:
            self._apply_preset(self.preset)

    # -- prefilling --------------------------------------------------------
    def _fill_existing(self, data: dict[str, Any]) -> None:
        self.query_one("#name", Input).value = str(self.existing.get("name", ""))
        pid = str(data.get("model_provider") or "")
        self.query_one("#provider_id", Input).value = pid
        self.query_one("#model", Input).value = str(data.get("model") or "")
        self.query_one("#effort", Input).value = str(data.get("model_reasoning_effort") or "")
        providers = data.get("model_providers")
        provider = providers.get(pid) if isinstance(providers, dict) and pid else None
        if isinstance(provider, dict):
            self.query_one("#base_url", Input).value = str(provider.get("base_url") or "")
            self.query_one("#model_catalog_url", Input).value = str(
                provider.get("model_catalog_url") or ""
            )
            token = provider.get("experimental_bearer_token")
            if isinstance(token, str) and token:
                self.query_one("#api_key", Input).value = token

    def _apply_preset(self, preset: ProviderPreset) -> None:
        self.preset = preset
        self.query_one("#provider_id", Input).value = preset.id
        name_input = self.query_one("#name", Input)
        if not name_input.disabled and not name_input.value.strip():
            name_input.value = preset.id
        if preset.official:
            self.query_one("#base_url", Input).value = ""
            self._hint("[yellow]正在加载官方模型目录 …[/yellow]")
            self._official_worker()
            return
        self.query_one("#base_url", Input).value = preset.base_url
        if not preset.native_responses:
            self._hint(
                "[yellow]该供应商仅支持 Chat Completions，Codex 0.16.x 需要本地 "
                "responses 代理转换后才能直接使用[/yellow]"
            )
        if preset.models:
            self._show_models(_preset_models(preset))

    # -- buttons -----------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "cancel":
            self.dismiss(None)
            return
        if button == "detect":
            self._start_detect()
            return
        if button == "provider":
            self._pick_provider()
            return
        self.dismiss(
            dict(
                {field: self.query_one(f"#{field}", Input).value.strip() for field in _FORM_FIELDS},
                _models=[
                    {
                        "slug": model.slug,
                        "display_name": model.display_name,
                        "levels": model.efforts,
                        "default": model.default_effort,
                    }
                    for model in self._known_models
                ],
            )
        )

    def _pick_provider(self) -> None:
        def done(preset: Optional[ProviderPreset]) -> None:
            if preset is None:
                return
            self._apply_preset(CUSTOM_PRESET if preset.id == "__custom__" else preset)

        self.app.push_screen(ProviderPickerScreen(), done)

    # -- hints / detection -------------------------------------------------
    def _hint(self, text: str) -> None:
        self.query_one("#hint", Label).update(text)

    def _start_detect(self) -> None:
        base_url = self.query_one("#base_url", Input).value.strip()
        catalog = self.query_one("#model_catalog_url", Input).value.strip()
        if not base_url and not catalog:
            self._hint("[red]请先填写 base_url 和/或 model_catalog_url[/red]")
            return
        typed = self.query_one("#api_key", Input).value.strip()
        api_key = typed
        self._hint("[yellow]正在检测模型 …[/yellow]")
        self._detect_worker(base_url, api_key, catalog)

    @work(thread=True, exclusive=True)
    def _detect_worker(self, base_url: str, api_key: str, catalog: str) -> None:
        result = detect_models(base_url or None, api_key, catalog or None)
        self.app.call_from_thread(self._detect_done, result)

    def _detect_done(self, result: Any) -> None:
        if not result.models:
            detail = "；".join(result.errors[:3]) or "未返回任何模型"
            self._hint(f"[red]检测失败：{detail}[/red]")
            return
        self._show_models(result.models)

    @work(thread=True, exclusive=True)
    def _official_worker(self) -> None:
        models = official_models()
        self.app.call_from_thread(self._official_done, models)

    def _official_done(self, models: list) -> None:
        detected = [
            DetectedModel(
                slug=str(entry["slug"]),
                display_name=str(entry.get("display_name") or entry["slug"]),
                efforts=list(entry.get("levels") or []),
                default_effort=entry.get("default") or None,
                source="official",
            )
            for entry in models
            if entry.get("slug")
        ]
        if not detected and self.preset is not None:
            detected = _preset_models(self.preset)
        if not detected:
            self._hint("[red]无法加载官方模型（是否已安装 codex？）[/red]")
            return
        self._show_models(detected)

    def _show_models(self, models: list[DetectedModel]) -> None:
        self._known_models = list(models)
        self._hint(f"[green]检测到 {len(models)} 个模型，请选择[/green]")
        self.app.push_screen(ModelPickerScreen(models), self._model_chosen)

    def _model_chosen(self, model: Optional[DetectedModel]) -> None:
        if model is None:
            return
        self._efforts = model.efforts
        self.query_one("#model", Input).value = model.slug
        if model.default_effort:
            self.query_one("#effort", Input).value = model.default_effort
        levels = ", ".join(model.efforts) or "未知（可手动填写）"
        self._hint(f"[green]{model.slug}[/green] — 推理等级：{levels}")


# --------------------------------------------------------------------------
# theme picker
# --------------------------------------------------------------------------
class ThemePickerScreen(ModalScreen[Optional[str]]):
    """Pick one of Textual's built-in themes, previewing live.

    Moving the cursor applies the highlighted theme to the whole app right
    away. Enter (or Apply) keeps it; Cancel/Escape restores the previous one.
    """

    CSS = """
    ThemePickerScreen { align: center middle; }
    #box { width: 60; height: 80%; padding: 1 2; background: $panel; border: round $primary; }
    #box Label.title { text-style: bold; padding-bottom: 1; }
    #themes { height: 1fr; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self) -> None:
        super().__init__()
        self._previous = "textual-dark"

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("主题 — 移动光标即预览，回车保留", classes="title")
            yield DataTable(id="themes", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")
                yield Button("应用", id="apply", variant="primary")

    def on_mount(self) -> None:
        self._previous = self.app.theme
        table = self.query_one("#themes", DataTable)
        table.add_columns("", "主题")
        current = self.app.theme
        names = sorted(self.app.available_themes)
        for name in names:
            table.add_row("*" if name == current else "", name, key=name)
        table.focus()
        if current in names:
            try:
                table.move_cursor(row=names.index(current))
            except Exception:
                pass

    def _preview(self, name: str) -> None:
        if name in self.app.available_themes:
            try:
                self.app.theme = name
            except Exception:
                pass

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self._preview(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.dismiss(str(event.row_key.value))

    def action_cancel(self) -> None:
        self._preview(self._previous)
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.dismiss(str(self.app.theme))
        else:
            self.action_cancel()


# --------------------------------------------------------------------------
# snapshots / confirm
# --------------------------------------------------------------------------
class SnapshotItem(ListItem):
    """快照列表里的一项（点击只高亮，不会恢复）。"""

    def __init__(self, key: str, text: str) -> None:
        super().__init__()
        self.entry_key = key
        self._text = text

    def compose(self) -> ComposeResult:
        yield Label(self._text)


class SnapshotsScreen(ModalScreen[Optional[str]]):
    """列出 config.toml 快照：回车恢复，d 删除。"""

    CSS = """
    SnapshotsScreen { align: center middle; }
    #box { width: 84; height: auto; max-height: 80%; padding: 1 2; background: $panel; border: round $primary; }
    #box Label.title { text-style: bold; padding-bottom: 1; }
    #snaps { height: 1fr; min-height: 6; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    SnapshotItem { height: auto; padding: 0 1; }
    SnapshotItem.-highlight { background: $boost; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("d", "delete", "删除"),
    ]

    # entry_key -> (kind, path)；kind 为 "baseline" | "snapshot" | "none"
    _by_key: dict[str, tuple[str, Optional[Path]]]

    def __init__(self) -> None:
        super().__init__()
        # ListView 鼠标点击也会发 Selected；用它区分“点击选中”与“回车恢复”。
        self._click_pending = False

    def on_click(self, event: Any) -> None:
        self._click_pending = True

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("快照：回车恢复，d 删除（点击仅选中）", classes="title")
            yield ListView(id="snaps")
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")
                yield Button("删除", id="delete", variant="error")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        list_view = self.query_one("#snaps", ListView)
        list_view.clear()
        self._by_key = {}
        items: list[ListItem] = []
        applied = (apply_mod.applied_status() or {}).get("snapshot")

        if apply_mod.has_baseline():
            items.append(SnapshotItem("__baseline__", "★ baseline（原始配置存档）"))
            self._by_key["__baseline__"] = ("baseline", None)

        snaps = apply_mod.list_snapshots()
        if not snaps and not items:
            items.append(SnapshotItem("__none__", "（暂无快照）"))
            self._by_key["__none__"] = ("none", None)
        for path in snaps:
            marker = "  ← 当前应用" if applied and str(path) == str(applied) else ""
            key = str(path)
            items.append(SnapshotItem(key, f"{path.name}{marker}"))
            self._by_key[key] = ("snapshot", path)

        list_view.extend(items)
        self.call_after_refresh(self._focus_first)

    def _focus_first(self) -> None:
        list_view = self.query_one("#snaps", ListView)
        list_view.focus()
        if list_view.children:
            list_view.index = 0

    def _selected(self) -> Optional[tuple[str, Optional[Path]]]:
        item = self.query_one("#snaps", ListView).highlighted_child
        if isinstance(item, SnapshotItem):
            return self._by_key.get(item.entry_key)
        return None

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # 鼠标点击会先触发 on_click，再发 Selected；此时只选中、不恢复。
        if self._click_pending:
            self._click_pending = False
            return
        item = event.item
        if not isinstance(item, SnapshotItem):
            return
        entry = self._by_key.get(item.entry_key)
        if not entry:
            return
        kind, path = entry
        if kind == "baseline":
            self.dismiss("__baseline__")
        elif kind == "snapshot" and path is not None:
            self.dismiss(str(path))

    def action_delete(self) -> None:
        entry = self._selected()
        if not entry or entry[0] == "none":
            return
        kind, path = entry
        applied = (apply_mod.applied_status() or {}).get("snapshot")
        label = "baseline（原始配置存档）" if kind == "baseline" else (path.name if path else "快照")
        note = ""
        if path is not None and applied and str(path) == str(applied):
            note = "\n（这是当前应用对应的快照，删除后无法再用 u 撤销）"

        def done(confirmed: bool) -> None:
            if not confirmed:
                return
            if kind == "baseline":
                apply_mod.delete_baseline()
            elif kind == "snapshot" and path is not None:
                apply_mod.delete_snapshot(path)
            self._refresh()

        self.app.push_screen(ConfirmScreen(f"删除 {label}？此操作不可撤销。{note}"), done)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete":
            self.action_delete()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SaveSnapshotScreen(ModalScreen[Optional[bool]]):
    """问是否先保存当前 config.toml 的快照。

    True = 保存并继续，False = 不保存继续，None = 取消。
    """

    CSS = """
    SaveSnapshotScreen { align: center middle; }
    #box { width: 68; height: auto; padding: 1 2; background: $panel; border: round $primary; }
    #box Label.title { text-style: bold; padding-bottom: 1; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, action_text: str) -> None:
        super().__init__()
        self.action_text = action_text

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"即将{self.action_text}", classes="title")
            yield Label(
                "是否先保存当前 config.toml 的快照？\n"
                "（不保存将无法精确回退到现在的状态）"
            )
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")
                yield Button("不保存", id="skip")
                yield Button("保存快照", id="save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.dismiss(True)
        elif event.button.id == "skip":
            self.dismiss(False)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    CSS = """
    ConfirmScreen { align: center middle; }
    #box { width: 60; height: auto; padding: 1 2; background: $panel; border: round $warning; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.message)
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")
                yield Button("确认", id="ok", variant="error")

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")

    def action_cancel(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------
# profile card
# --------------------------------------------------------------------------
class ProfileCard(ListItem):
    """一个 profile（供应商）的卡片，可点击选中。"""

    def __init__(
        self,
        name: str,
        summary: dict[str, str],
        active: bool,
        applied: bool,
        model_count: int = 0,
    ) -> None:
        super().__init__()
        self.profile_name = name
        self._summary = summary
        self._active = active
        self._applied = applied
        self._model_count = model_count

    def compose(self) -> ComposeResult:
        model = self._summary.get("model", "-")
        effort = self._summary.get("model_reasoning_effort", "-")
        line = Text()
        line.append(self.profile_name, style="bold")
        if self._active:
            line.append("  ★激活", style="green")
        if self._applied:
            line.append("  ●已应用", style="cyan")
        line.append("   ")
        line.append(model, style="dim")
        line.append(f" · {effort}", style="dim")
        if self._model_count:
            line.append(f" · {self._model_count} 模型", style="dim")
        yield Label(line)


# --------------------------------------------------------------------------
# session manager / usage
# --------------------------------------------------------------------------
def _fmt_time(seconds: int) -> str:
    if not seconds:
        return "-"
    import datetime

    try:
        return datetime.datetime.fromtimestamp(seconds).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


class SessionItem(ListItem):
    """One local Codex session row."""

    def __init__(self, info: sessions_mod.SessionInfo) -> None:
        super().__init__()
        self.info = info

    def compose(self) -> ComposeResult:
        s = self.info
        line = Text()
        line.append("⚠ " if s.needs_sync else "  ", style="yellow")
        line.append((s.label or s.id)[:56], style="bold")
        if s.archived:
            line.append("  [归档]", style="dim")
        if s.is_subagent:
            line.append("  [子]", style="dim")
        line.append("   ")
        line.append(s.provider or "-", style="cyan")
        line.append(f" · {s.model or '-'}", style="dim")
        line.append(f" · {_fmt_time(s.updated_at)}", style="dim")
        if s.tokens_used:
            line.append(f" · {s.tokens_used} tok", style="dim")
        yield Label(line)


class SessionsScreen(ModalScreen[Optional[str]]):
    """List local sessions; sync provider metadata / delete / resume."""

    CSS = """
    SessionsScreen { align: center middle; }
    #box { width: 110; height: 85%; padding: 1 2; background: $panel; border: round $primary; }
    #box Label.title { text-style: bold; padding-bottom: 1; }
    #sessions { height: 1fr; }
    #sess-status { height: auto; color: $text-muted; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    #buttons Button { margin-left: 2; }
    SessionItem { height: auto; padding: 0 1; }
    SessionItem.-highlight { background: $boost; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("d", "delete", "删除"),
        Binding("r", "refresh", "刷新"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._click_pending = False
        self._infos: list[sessions_mod.SessionInfo] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("会话管理", classes="title", id="sess-title")
            yield ListView(id="sessions")
            yield Label("", id="sess-status")
            with Horizontal(id="buttons"):
                yield Button("取消", id="cancel")
                yield Button("删除", id="delete", variant="error")

    def on_mount(self) -> None:
        self._refresh()

    def _set_status(self, text: str) -> None:
        self.query_one("#sess-status", Label).update(text)

    def _refresh(self) -> None:
        self._infos = sessions_mod.list_sessions()
        target = sessions_mod.current_provider()
        self.query_one("#sess-title", Label).update(
            f"会话管理 · 当前 provider: {target} · 共 {len(self._infos)} 个"
            f"（切换供应商后自动同步）"
        )
        list_view = self.query_one("#sessions", ListView)
        list_view.clear()
        if not self._infos:
            list_view.extend([ListItem(Label("没有本地会话", classes="empty"))])
            return
        list_view.extend([SessionItem(info) for info in self._infos])
        self.call_after_refresh(self._focus_first)

    def _focus_first(self) -> None:
        list_view = self.query_one("#sessions", ListView)
        list_view.focus()
        if list_view.children:
            list_view.index = 0

    def _selected_info(self) -> sessions_mod.SessionInfo | None:
        item = self.query_one("#sessions", ListView).highlighted_child
        return item.info if isinstance(item, SessionItem) else None

    def on_click(self, event: Any) -> None:
        self._click_pending = True

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._click_pending:
            self._click_pending = False
            return
        info = self._selected_info()
        if info is not None:
            self.dismiss(f"resume:{info.id}")

    def action_delete(self) -> None:
        info = self._selected_info()
        if info is None:
            return

        def done(confirmed: bool) -> None:
            if not confirmed:
                return
            result = sessions_mod.delete_sessions([info.id])
            self._set_status(f"已删除会话（备份：{result['backup']}）")
            self._refresh()

        self.app.push_screen(ConfirmScreen(f"永久删除会话「{info.label}」？不可恢复。"), done)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete":
            self.action_delete()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
    def action_refresh(self) -> None:
        self._refresh()


class UsageScreen(ModalScreen[None]):
    """Token usage statistics from rollout files."""

    CSS = """
    UsageScreen { align: center middle; }
    #box { width: 96; height: 85%; padding: 1 2; background: $panel; border: round $primary; }
    #box Label.title { text-style: bold; padding-bottom: 1; }
    #usage { height: 1fr; }
    #buttons { height: auto; align-horizontal: right; padding-top: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("Token 用量统计", classes="title")
            with VerticalScroll(id="usage"):
                yield Static(id="usage-body")
            with Horizontal(id="buttons"):
                yield Button("关闭", id="close")

    def on_mount(self) -> None:
        self.query_one("#usage-body", Static).update(self._usage_text())

    def _usage_text(self) -> Text:
        report = usage_mod.scan_usage()
        t = report.total
        out = Text()
        out.append("总计\n", style="bold")
        out.append(
            f"  {t.total:,} tokens  (输入 {t.input:,} · 缓存 {t.cached:,} · "
            f"输出 {t.output:,} · 推理 {t.reasoning:,})\n"
            f"  缓存命中率 {t.cache_rate * 100:.1f}% · 会话 {len(report.sessions)} 个\n"
        )
        if report.by_model:
            out.append("\n按模型\n", style="bold")
            for model, tt in sorted(
                report.by_model.items(), key=lambda kv: kv[1].total, reverse=True
            ):
                out.append(
                    f"  {model:<28} {tt.total:>12,}  (输入 {tt.input:,} / 输出 {tt.output:,})\n"
                )
        if report.by_day:
            out.append("\n按日期（最近 14 天）\n", style="bold")
            for day, tt in sorted(report.by_day.items())[-14:]:
                out.append(f"  {day}  {tt.total:>12,}  (缓存 {tt.cached:,})\n")
        if report.sessions:
            out.append("\n主会话（按用量）\n", style="bold")
            for usage in report.sessions[:15]:
                out.append(
                    f"  {usage.totals.total:>12,}  {usage.info.model:<22} "
                    f"{(usage.info.label or usage.info.id)[:40]}\n"
                )
        return out

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# main app
# --------------------------------------------------------------------------
class CxApp(App):
    TITLE = "codex-profiles"

    CSS = """
    #body { height: 1fr; }
    #cards { width: 60%; border: round $primary; padding: 0 1; }
    #right { width: 40%; border: round $secondary; }
    #status { height: auto; padding: 0 1; background: $panel; }
    #detail { height: 1fr; }
    #detail-body { padding: 0 1; }
    #log { height: 10; border-top: solid $primary; }
    ProfileCard {
        height: auto;
        margin: 0;
        padding: 0 1;
        border: round $panel;
    }
    ProfileCard.-highlight {
        background: $boost;
        border: round $accent;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "刷新"),
        Binding("a", "add", "新增"),
        Binding("e", "edit", "编辑"),
        Binding("E", "edit_raw", "原始TOML"),
        Binding("d", "delete", "删除"),
        Binding("space", "activate", "设为激活"),
        Binding("x", "apply", "应用到config"),
        Binding("u", "unapply", "撤销应用"),
        Binding("s", "snapshots", "快照"),
        Binding("R", "restore_original", "还原原始"),
        Binding("S", "sessions", "会话"),
        Binding("U", "usage", "用量"),
        Binding("v", "verify", "验证"),
        Binding("l", "launch", "启动"),
        Binding("t", "themes", "主题"),
        Binding("q", "quit", "退出"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._names: list[str] = []
        self._selected: str | None = None

    # -- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield ListView(id="cards")
            with Vertical(id="right"):
                yield Static(id="status")
                with VerticalScroll(id="detail"):
                    yield Static(id="detail-body")
                yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(CODEX_THEME)
        self.theme = state.get_theme() or DEFAULT_THEME
        self.refresh_profiles()

    # -- helpers -----------------------------------------------------------
    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(redact_text(message))

    def _prompt_snapshot(self, action_text: str, callback: Callable[[bool], None]) -> None:
        """先问“是否保存快照”，再执行修改 config.toml 的操作。

        若当前配置已经有内容相同的快照，就不再问（不会重复保存）。
        """
        if apply_mod.current_config_snapshot() is not None:
            callback(True)
            return

        def done(choice: Optional[bool]) -> None:
            if choice is None:
                return
            callback(choice)

        self.push_screen(SaveSnapshotScreen(action_text), done)

    def _after_provider_change(self) -> None:
        """config.toml 的 provider 变了：后台重启守护进程 + 自动同步会话。"""
        self._log("[dim]正在重启守护进程并同步会话 …[/dim]")
        self.run_worker(self._do_provider_change, thread=True, group="provider-change")

    def _do_provider_change(self) -> None:
        ok, message = restart_app_server_daemon()
        if ok:
            self.call_from_thread(
                self._log, "[green]已重启 app-server 守护进程（使新模型目录生效）[/green]"
            )
        else:
            self.call_from_thread(self._log, f"[dim]未重启守护进程：{message}[/dim]")
        try:
            ids = [s.id for s in sessions_mod.list_sessions() if s.needs_sync]
            if ids:
                result = sessions_mod.sync_provider(ids)
                self.call_from_thread(
                    self._log,
                    f"[green]已自动同步 {len(ids)} 个会话到 {result['target']}[/green]",
                )
            else:
                self.call_from_thread(self._log, "[dim]会话 provider 已一致[/dim]")
        except Exception as exc:
            self.call_from_thread(self._log, f"[yellow]自动同步会话失败：{exc}[/yellow]")
        self.call_from_thread(self.refresh_profiles, self._selected)

    def _current(self) -> str | None:
        if self._selected and self._selected in self._names:
            return self._selected
        return self._names[0] if self._names else None

    def _update_status(self) -> None:
        info = apply_mod.applied_status()
        codex_bin = find_codex()
        codex_txt = (
            f"[green]{codex_bin}[/green] ({codex_version()})"
            if codex_bin
            else "[red]未在 PATH 中找到[/red]"
        )
        applied = info.get("profile") if info else None
        active = state.get_active()
        lines = [
            f"[b]CODEX_HOME[/b] {codex_home()}",
            f"[b]codex[/b]      {codex_txt}",
            f"[b]当前激活[/b]  {active or '-'}",
            f"[b]已应用[/b]    {applied or '-'}"
            + (f"  [dim](快照: {info.get('snapshot') or '无'})[/dim]" if applied else ""),
            f"[b]主题[/b]      {self.theme}",
        ]
        self.query_one("#status", Static).update("\n".join(lines))

    def refresh_profiles(self, keep: str | None = None) -> None:
        self._names = list_profile_names()
        target = keep or self._current()
        self._selected = target if target in self._names else (self._names[0] if self._names else None)
        self._rebuild_cards(self._selected)
        self._update_status()
        self._show_detail()

    @work(exclusive=True)
    async def _rebuild_cards(self, target: str | None) -> None:
        list_view = self.query_one("#cards", ListView)
        await list_view.clear()
        active = state.get_active()
        applied = (apply_mod.applied_status() or {}).get("profile")
        cards = [
            ProfileCard(
                name,
                summarize(name),
                name == active,
                name == applied,
                len(catalog_mod.models_for_profile(name)),
            )
            for name in self._names
        ]
        if not cards:
            await list_view.extend([ListItem(Label("还没有 profile，按 a 新增", classes="empty"))])
            return
        await list_view.extend(cards)
        if target in self._names:
            list_view.index = self._names.index(target)
        else:
            list_view.index = 0
        list_view.focus()

    def _show_detail(self) -> None:
        name = self._current()
        detail = self.query_one("#detail-body", Static)
        if not name:
            detail.update("[dim]还没有 profile，按 [b]a[/b] 新增[/dim]")
            return
        try:
            text = load_text(name)
        except ProfileError as exc:
            detail.update(f"[red]{exc}[/red]")
            return
        base = read_base_config()
        try:
            data = load_dict(name)
        except Exception:
            data = {}
        problems = validate_document(data) + provider_reference_issues(data, base)
        header = Text.from_markup(f"[b]{name}.config.toml[/b]")
        if problems:
            header.append("\n" + "\n".join(f"! {p}" for p in problems), style="yellow")

        parts: list[Any] = [header]
        models = catalog_mod.models_for_profile(name)
        if models:
            parts.append(Text(f"\n模型（{len(models)}）：", style="bold"))
            for model in models[:200]:
                levels = ", ".join(model.get("levels") or []) or "-"
                default = f"  默认 {model['default']}" if model.get("default") else ""
                parts.append(Text(f"  • {model.get('slug')}  [{levels}]{default}"))
            if len(models) > 200:
                parts.append(Text(f"  … 还有 {len(models) - 200} 个", style="dim"))
        parts.append(Syntax(redact_toml(text).rstrip(), "toml", word_wrap=True))
        detail.update(Group(*parts))

    # -- events ------------------------------------------------------------
    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, ProfileCard):
            self._selected = item.profile_name
            self._show_detail()

    # -- profile actions ---------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_profiles(keep=self._selected)
        self._log("[dim]已刷新[/dim]")

    def action_activate(self) -> None:
        name = self._current()
        if not name:
            return
        state.set_active(name)
        self._log(f"[green]已设为激活：{name}[/green]")
        self.refresh_profiles(keep=name)

    def action_add(self) -> None:
        def after_preset(preset: Optional[ProviderPreset]) -> None:
            if preset is None:
                return
            chosen = None if preset.id == "__custom__" else preset
            self.push_screen(ProfileFormScreen(preset=chosen), self._save_profile)

        self.push_screen(ProviderPickerScreen(), after_preset)

    def action_edit(self) -> None:
        name = self._current()
        if not name:
            return
        try:
            data = load_dict(name)
        except Exception as exc:
            self._log(f"[red]无法读取 {name}：{exc}[/red]")
            return
        self.push_screen(
            ProfileFormScreen(existing={"name": name, "data": data}),
            lambda values: self._save_profile(values, existing_name=name),
        )

    def action_edit_raw(self) -> None:
        name = self._current()
        if not name:
            return
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        with self.suspend():
            subprocess.call([editor, str(profile_path(name))])
        self.refresh_profiles(keep=name)
        self._log(f"[green]已在 $EDITOR 中编辑 {name}[/green]")

    def _save_profile(
        self, values: Optional[dict[str, Any]], existing_name: Optional[str] = None
    ) -> None:
        if not values:
            return
        name = str(values["name"])
        base_url = str(values.get("base_url") or "").strip()
        model = str(values.get("model") or "").strip()
        api_key = str(values.get("api_key") or "").strip()

        # Keep the profile's model catalog in sync so Codex's /model picker shows
        # exactly these models (custom providers only).
        catalog_json: Optional[str] = None
        if base_url:
            models = list(values.get("_models") or [])
            if not models and existing_name:
                try:
                    prior = load_dict(existing_name).get("model_catalog_json")
                except Exception:
                    prior = None
                if prior:
                    models = catalog_mod.models_from_catalog(str(prior))
            if not models:
                preset = find_preset(str(values.get("provider_id") or ""))
                if preset is not None:
                    models = [
                        {
                            "slug": m.slug,
                            "display_name": m.display_name,
                            "levels": list(m.levels),
                            "default": m.default,
                            "context": m.context,
                        }
                        for m in preset.models
                    ]
            if model and all(m.get("slug") != model for m in models):
                models.insert(
                    0, {"slug": model, "display_name": model, "levels": [], "default": None}
                )
            written = catalog_mod.write_catalog(existing_name or name, models)
            if written is not None:
                catalog_json = str(written)

        fresh = build_profile_document(
            name=name,
            model=model or None,
            effort=values.get("effort") or None,
            provider_id=values.get("provider_id") or None,
            base_url=base_url or None,
            api_key=api_key or None,
            model_catalog_url=values.get("model_catalog_url") or None,
            model_catalog_json=catalog_json,
        )
        if not fresh:
            self._log("[red]没有可写入的内容：请填写模型或 base_url[/red]")
            return

        if existing_name:
            try:
                doc = load_dict(existing_name)
            except Exception:
                doc = {}
            for key in _MANAGED_KEYS:
                if key in fresh:
                    doc[key] = fresh[key]
                else:
                    doc.pop(key, None)
            if "model_providers" in fresh:
                providers = doc.setdefault("model_providers", {})
                if isinstance(providers, dict):
                    providers.update(fresh["model_providers"])
            target = existing_name
        else:
            doc = fresh
            target = name

        try:
            doc = self._validated(target, doc)
            write_profile(target, doc)
            state.set_active(target)
            if catalog_json:
                self._log(f"[green]已保存 {target}[/green]（含模型目录，/model 可见）")
            else:
                self._log(f"[green]已保存 {target}[/green]")
            self.refresh_profiles(keep=target)
        except ProfileError as exc:
            self._log(f"[red]{exc}[/red]")
        except ValueError as exc:
            self._log(f"[red]{exc}[/red]")

    def _validated(self, name: str, doc: dict[str, Any]) -> dict[str, Any]:
        problems = validate_document(doc) + provider_reference_issues(doc, read_base_config())
        if problems:
            raise ValueError("配置无效：" + "；".join(problems))
        return doc

    def action_delete(self) -> None:
        name = self._current()
        if not name:
            return

        def done(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                dest = delete_profile(name)
                # Keep the catalog if config.toml still points at it, otherwise
                # Codex would fail to load a missing model_catalog_json.
                base = read_base_config() or {}
                referenced = str(base.get("model_catalog_json") or "")
                if referenced != str(catalog_mod.catalog_path(name)):
                    catalog_mod.remove_catalog(name)
                else:
                    self._log(
                        "[yellow]该 profile 的模型目录仍被 config.toml 引用，已保留目录文件[/yellow]"
                    )
                if state.get_active() == name:
                    state.set_active(None)
                self._log(f"[green]已删除 {name}[/green] -> {dest}")
                self.refresh_profiles()
            except ProfileError as exc:
                self._log(f"[red]{exc}[/red]")

        self.push_screen(ConfirmScreen(f"删除 profile「{name}」？"), done)

    # -- config.toml apply / restore --------------------------------------
    def action_apply(self) -> None:
        name = self._current()
        if not name:
            return

        def proceed(save: bool) -> None:
            try:
                catalog_mod.ensure_catalog(name)
                path = apply_mod.apply_profile(name, save_snapshot=save)
                self._log(f"[green]已应用 {name}[/green] -> {path}（普通 codex 已生效）")
                self._after_provider_change()
                self.refresh_profiles(keep=name)
            except Exception as exc:
                self._log(f"[red]{exc}[/red]")

        self._prompt_snapshot(f"应用 {name}", proceed)

    def action_unapply(self) -> None:
        def proceed(save: bool) -> None:
            try:
                path = apply_mod.unapply(save_snapshot=save)
                self._log(f"[green]已恢复[/green] {path or '（已移除 config.toml）'}")
                self._after_provider_change()
                self.refresh_profiles(keep=self._selected)
            except Exception as exc:
                self._log(f"[red]{exc}[/red]")

        self._prompt_snapshot("撤销应用", proceed)

    def action_restore_original(self) -> None:
        def proceed(save: bool) -> None:
            try:
                path = apply_mod.restore_original(save_snapshot=save)
                self._log(
                    f"[green]已还原到 Codex 原始配置[/green] "
                    f"{path or '（已移除 config.toml）'}"
                )
                self._after_provider_change()
                self.refresh_profiles(keep=self._selected)
            except Exception as exc:
                self._log(f"[red]{exc}[/red]")

        self._prompt_snapshot(
            "还原到 Codex 原始配置（移除 cx 写入的 model/model_provider/"
            "model_reasoning_effort/model_catalog_json 与 cx 添加的 provider，其余保留）",
            proceed,
        )

    def action_snapshots(self) -> None:
        def chosen(value: Optional[str]) -> None:
            if not value:
                return
            if value == "__baseline__":
                def proceed(save: bool) -> None:
                    try:
                        path = apply_mod.restore_original(save_snapshot=save)
                        self._log(
                            f"[green]已还原原始配置[/green] "
                            f"{path or '（已移除 config.toml）'}"
                        )
                        self._after_provider_change()
                        self.refresh_profiles(keep=self._selected)
                    except Exception as exc:
                        self._log(f"[red]{exc}[/red]")

                self._prompt_snapshot("还原原始配置", proceed)
                return

            snapshot = Path(value)

            def proceed(save: bool) -> None:
                try:
                    restored = apply_mod.restore_snapshot(snapshot, save_snapshot=save)
                    self._log(
                        f"[green]已从快照 {snapshot.name} 恢复[/green] -> {restored}"
                    )
                    self._after_provider_change()
                    self.refresh_profiles(keep=self._selected)
                except Exception as exc:
                    self._log(f"[red]{exc}[/red]")

            self._prompt_snapshot(f"从快照 {snapshot.stem} 恢复", proceed)

        self.push_screen(SnapshotsScreen(), chosen)

    # -- theme -------------------------------------------------------------
    def action_themes(self) -> None:
        def done(name: Optional[str]) -> None:
            if not name:
                return
            try:
                self.theme = name
                state.set_theme(name)
                self._log(f"[green]主题 -> {name}[/green]")
                self._update_status()
            except Exception as exc:
                self._log(f"[red]无法应用主题 {name}：{exc}[/red]")

        self.push_screen(ThemePickerScreen(), done)

    # -- session manager / usage -------------------------------------------
    def action_sessions(self) -> None:
        def done(value: Optional[str]) -> None:
            if isinstance(value, str) and value.startswith("resume:"):
                self._resume_session(value[len("resume:") :])
            self.refresh_profiles(keep=self._selected)

        self.push_screen(SessionsScreen(), done)

    def _resume_session(self, session_id: str) -> None:
        codex_bin = find_codex()
        if not codex_bin:
            self._log("[red]PATH 中未找到 codex[/red]")
            return
        active = state.get_active()
        args = [codex_bin]
        if active:
            args += ["--profile", active]
        args += ["resume", session_id]
        self._log(f"[green]恢复会话 {session_id[:8]} …[/green]")
        with self.suspend():
            subprocess.call(args)
        self.refresh_profiles(keep=self._selected)

    def action_usage(self) -> None:
        self.push_screen(UsageScreen())

    # -- codex verify / launch --------------------------------------------
    def action_verify(self) -> None:
        name = self._current()
        if not name:
            return
        self._log(f"[dim]正在用 codex 验证 {name} …[/dim]")
        self._verify_worker(name)

    @work(thread=True, exclusive=True)
    def _verify_worker(self, name: str) -> None:
        result = probe_profile(name)
        self.call_from_thread(self._verify_done, name, result)

    def _verify_done(self, name: str, result: Any) -> None:
        if not result.ok:
            self._log(f"[red]验证失败 {name}：{result.error}[/red]")
            if result.output:
                self._log(f"[dim]{result.output[-500:]}[/dim]")
            return
        h = result.header
        self._log(
            f"[green]验证通过[/green] {name}：model={h.get('model')} "
            f"provider={h.get('provider')} effort={h.get('reasoning_effort')}"
        )

    def action_launch(self) -> None:
        name = self._current()
        if not name:
            return
        codex_bin = find_codex()
        if not codex_bin:
            self._log("[red]PATH 中未找到 codex[/red]")
            return
        self._log(f"[green]启动 codex --profile {name}[/green]")
        catalog_mod.ensure_catalog(name)
        with self.suspend():
            subprocess.call([codex_bin, "--profile", name])
        self.refresh_profiles(keep=name)


def run_tui() -> None:
    CxApp().run()


if __name__ == "__main__":
    run_tui()
