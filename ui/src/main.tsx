import React, { useState, useEffect, useRef, useCallback } from "react";
import { createRoot } from "react-dom/client";
import {
  Sun,
  CalendarDays,
  Flag,
  Layers,
  Check,
  CheckCheck,
  Trash2,
  UserRound,
  List,
  Columns3,
  Search,
  Plus,
  Settings2,
  ChevronRight,
  ChevronLeft,
  ChevronDown,
  MoreHorizontal,
  X,
  ArrowUpRight,
  Paperclip,
  Link2,
  Repeat2,
  Clock,
  MapPin,
  Command,
  RefreshCw,
  Undo2,
  MessageSquare,
  PanelRight,
  Download,
  Folder,
  Tag,
  Sparkles,
  ArrowUpDown,
  CornerDownLeft,
  Copy,
  Pin,
  SlidersHorizontal,
  FileText,
  CheckCircle2,
  AlertCircle,
  Bell,
  Radio,
  Info,
  PanelLeft,
} from "lucide-react";
import {
  app,
  extensions,
  ready,
  call,
  mutate,
  attach,
  attachImages,
  discuss,
  safeLink,
  subscribe,
  RecordData as D,
} from "./bridge";
import {
  ListBadge,
  listChoices,
  ContextMenu,
  CommandPalette,
  ListAppearance,
  Action,
  useInternalDrag,
} from "./interactions";
import {
  arrangeReminders,
  selectionRange,
  rescheduledDue,
  recurrenceText,
  earlyReminderText,
  isReminder,
  todaySection,
  systemListVisible,
  pinnedSidebarLists,
  sidebarLists,
  sidebarKey,
  sidebarScope,
  orderSidebarItems,
  moveSidebarItem,
  colorFor,
  COLORS,
} from "./reminder-helpers";
import {TagEditor, AttachmentGallery, SmartListEditor} from "./power";
import {DueEditor} from "./date-editor";
import {QuickAdd} from "./quick-add";
import {Select} from "./pickers";
import {NotePreview, RichLinks} from "./rich-content";
import icon from "../../plugins/remctl/assets/icon.png";
import "./style.css";
const VIEWS = [
  ["today", "Today", Sun, "blue"],
  ["scheduled", "Scheduled", CalendarDays, "red"],
  ["flagged", "Flagged", Flag, "orange"],
  ["all", "All", Layers, "graphite"],
  ["completed", "Completed", CheckCheck, "gray"],
] as const;
// Apple's system hues for the built-in views, as in Reminders.
const VIEW_COLORS: Record<string, string> = {
  today: "#0a7cff", scheduled: "#f0453a", flagged: "#ff9500", all: "#5b5f68",
  completed: "#8a8d93", assigned: "#30b456", deleted: "#8a8d93",
};
const today = () => new Date().toLocaleDateString("en-CA");
const titleCase = (s: string) =>
  s.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
function dateText(item: D) {
  const shownDate = item.displayDate || item.dueDate;
  if (!shownDate) return "";
  const day = shownDate.slice(0, 10);
  const now = today();
  const label =
    day === now
      ? "Today"
      : new Date(day + "T12:00:00").toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
        });
  return (
    label +
    (item.allDay
      ? ""
      : " · " +
        new Date(shownDate).toLocaleTimeString(undefined, {
          hour: "numeric",
          minute: "2-digit",
        }))
  );
}
function IconButton({
  label,
  children,
  onClick,
  active = false,
  disabled = false,
}: {
  label: string;
  children: React.ReactNode;
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      className={"icon-button " + (active ? "active" : "")}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
function Workspace() {
  const [data, setData] = useState<D>({
      items: [],
      lists: [],
      sections: [],
      smartLists: [],
      counts: {},
    }),
    [query, setQuery] = useState<D>({ view: "today" }),
    [settings, setSettings] = useState<D>({
      layout: "list",
      density: "comfortable",
      weekStartsOn: "monday",
      refreshSeconds: 30,
    }),
    [layout, setLayout] = useState("list");
  // "System" follows whichever changed last: the host's theme or the Mac's
  // appearance. Some hosts never send a theme change after launch.
  const [hostTheme, setHostTheme] = useState(() =>
    window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  useEffect(() => {
    const dark = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = () => setHostTheme(dark.matches ? "dark" : "light");
    dark.addEventListener("change", follow);
    return () => dark.removeEventListener("change", follow);
  }, []);
  const reportedTheme = useRef<string>(undefined);
  const [loading, setLoading] = useState(true),
    [readFailed, setReadFailed] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [toast, setToast] = useState(""),
    [detail, setDetail] = useState<D | null>(null),
    [selection, setSelection] = useState<number[]>([]),
    [attached, setAttached] = useState<number[]>([]),
    [attachedDetails, setAttachedDetails] = useState<D[]>([]),
    [surface, setSurface] = useState("workspace"),
    [modal, setModal] = useState<D | null>(null),
    [catalog, setCatalog] = useState<D[]>([]),
    [symbols, setSymbols] = useState<D>({}),
    [search, setSearch] = useState(""),
    [quickDraft, setQuickDraft] = useState<D | null>(null),
    [quickOpen, setQuickOpen] = useState(false),
    [undo, setUndo] = useState<D | null>(null),
    [sidebar, setSidebar] = useState(window.innerWidth > 1100),
    [file, setFile] = useState<D | null>(null),
    [month, setMonth] = useState(new Date()),
    [menu, setMenu] = useState(false),
    [collapsed, setCollapsed] = useState<number[]>([]),
    [collapsedSections, setCollapsedSections] = useState<string[]>([]),
    [collapsedGroups, setCollapsedGroups] = useState<number[]>([]),
    [sidebarOrder, setSidebarOrder] = useState<Record<string, string[]> | null>(null);
  useEffect(() => {
    const wide = window.matchMedia("(min-width: 1101px)");
    const resize = () => setSidebar(wide.matches);
    wide.addEventListener("change", resize);
    return () => wide.removeEventListener("change", resize);
  }, []);
  const drafts = useRef(new Map<number, {fields: D; revision: string}>());
  const layoutKey = (q: D) => q.listId ? `list:${q.listId}` : q.smartId ? `smart:${q.smartId}` : `view:${q.view || "today"}`;
  // The sandbox may refuse storage; layouts then last for this session only.
  const savedLayouts = useRef<Record<string, string>>((() => {
    try { return JSON.parse(localStorage.getItem("remctl-layouts") || "{}"); } catch { return {}; }
  })());
  const defaultLayout = useRef("list");
  const layoutFor = (q: D) => savedLayouts.current[layoutKey(q)] || defaultLayout.current;
  const chooseLayout = (id: string) => {
    setLayout(id);
    savedLayouts.current[layoutKey(queryRef.current)] = id;
    try { localStorage.setItem("remctl-layouts", JSON.stringify(savedLayouts.current)); } catch {}
  };
  const selectionAnchor = useRef<number | null>(null);
  const selectionFocus = useRef<number | null>(null);
  const draggingIds = useRef<number[]>([]);
  const request = useRef(0),
    queryRef = useRef(query),
    dataRef = useRef(data),
    detailRef = useRef(detail),
    initialized = useRef(false),
    searchRef = useRef<HTMLInputElement>(null);
  queryRef.current = query;
  dataRef.current = data;
  detailRef.current = detail;
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    items: Action[];
  } | null>(null);
  const report = (err: any) => setError(/Transport closed|Connection closed/i.test(err?.message || String(err)) ? "RemCTL disconnected. Reopen Reminders from the app sidebar to reconnect." : err?.message || String(err));
  const busyRef = useRef(false);
  const run = async (fn: () => Promise<any>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setError("");
    setBusy(true);
    try {
      return await fn();
    } catch (err) {
      report(err);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };
  const refresh = useCallback(
    async (next = queryRef.current, append = false) => {
      const seq = ++request.current;
      setLoading(true);
      try {
        const clean = Object.fromEntries(
          Object.entries(next).filter(
            ([, value]) => value !== null && value !== undefined,
          ),
        );
        const value = await call("workspace_query", clean);
        if (seq === request.current) {
          if (
            append &&
            dataRef.current.snapshot &&
            value.snapshot !== dataRef.current.snapshot
          ) {
            setToast("Reminders changed. The list was refreshed.");
            await refresh({ ...next, offset: 0 });
            return;
          }
          setData((old) =>
            append
              ? { ...value, items: [...old.items, ...value.items] }
              : value,
          );
          setReadFailed(false);
          setError("");
        }
      } catch (err) {
        if (seq === request.current) {setReadFailed(true);report(err);}
      } finally {
        if (seq === request.current) setLoading(false);
      }
    },
    [],
  );
  const navigate = (next: D) => {
    if (window.innerWidth <= 1100) setSidebar(false);
    queryRef.current = next;
    setData((old) => ({ ...old, items: [], total: undefined, nextOffset: null }));
    setReadFailed(false);
    setCollapsedSections([]);
    setQuery(next);
    setSearch("");
    setDetail(null);
    setSelection([]);
    setSurface("workspace");
    setLayout(layoutFor(next));
    refresh(next);
  };
  const openDetail = async (item: D) => {
    if (queryRef.current.view === "deleted") {
      setDetail(item);
      return;
    }
    setDetail(item);
    try {
      const full = await call("workspace_detail", {
        identifier: item.objectUUID || item.id,
      });
      setDetail((current) => (current?.id === item.id ? full : current));
    } catch (err) {
      report(err);
    }
  };
  useEffect(
    () =>
      subscribe((event) => {
        if (event.type === "error") {
          setLoading(false);
          setError(event.message);
        }
        if (event.type === "file") {
          setFile(event.file);
          setSurface("file");
          setLoading(false);
        }
        if (event.type === "result") {
          const result = event.result,
            value = result.structuredContent || {};
          if (result.isError) {
            setLoading(false);
            report(
              new Error(
                value.message ||
                  value.error?.message ||
                  result.content?.[0]?.text,
              ),
            );
            return;
          }
          if (value.surface === "file") {
            setFile(value.file);
            setSurface("file");
            setLoading(false);
            return;
          }
          if (value.surface === "selection") {
            setSurface("selection");
            setData((old) => ({ ...old, ...value }));
            setLoading(false);
          } else if (Array.isArray(value.items) && (value.lists || value.items.every(isReminder))) {
            if (!value.lists) setSurface("inline");
            setData((old) => ({ ...old, ...value }));
            setQuery((old) => ({
              ...old,
              view: value.view || old.view,
              listId: value.listId ?? undefined,
            }));
            setLoading(false);
          } else if (isReminder(value)) {
            setData((old) => ({ ...old, items: [value], total: 1 }));
            setSurface("inline");
            setLoading(false);
          } else {
            setSurface("workspace");
            refresh();
          }
          const preferences = result._meta?.["remctl/settings"];
          if (preferences) {
            setSettings(preferences);
            defaultLayout.current = preferences.layout || "list";
            setLayout(layoutFor(queryRef.current));
          }
          if (!initialized.current) {
            initialized.current = true;
            call("read_settings")
              .then((s) => {
                setSettings(s.values);
                defaultLayout.current = s.values.layout || "list";
                setLayout(layoutFor(queryRef.current));
              })
              .catch(report);
            call("workspace_catalog")
              .then((s) => {
                setCatalog(s.tools);
                setSymbols(s.symbols || {});
              })
              .catch(report);
            call("read_sidebar_order").then(s => setSidebarOrder(s.orders)).catch(report);
          }
        }
        if (event.type === "context") {
          const theme = event.context?.theme;
          if (theme && theme !== reportedTheme.current) {
            reportedTheme.current = theme;
            setHostTheme(theme);
          }
          const current = extensions.modelContext?.getCurrent();
          if (current !== undefined) {
            const allItems = (current?.structuredContent?.items as D[]) || [];
            const remaining = current?.content
              ?.map((block: any) => block._meta?.["remctl/id"])
              .filter((id: any) => typeof id === "number");
            const items = remaining
              ? allItems.filter((i) => remaining.includes(i.id))
              : allItems;
            setAttached(items.map((i) => i.id));
            setAttachedDetails(items);
          }
          const route = extensions.deepLink.getCurrent()?.url;
          if (route) {
            const parts = route.split("/").filter(Boolean);
            if (parts[0] === "reminder" && parts[1])
              call("workspace_detail", {
                identifier: decodeURIComponent(parts[1]),
              })
                .then(setDetail)
                .catch(report);
            else if (parts[0] === "list" && /^\d+$/.test(parts[1]))
              navigate({ view: "list", listId: Number(parts[1]) });
            else if (
              ["today", "scheduled", "flagged", "all"].includes(parts[0])
            )
              navigate({ view: parts[0] });
          }
        }
      }),
    [],
  );
  useEffect(() => {
    let cancelled = false;
    if (!attached.length) {
      setAttachedDetails([]);
      return;
    }
    Promise.all(attached.map((id) => call("workspace_detail", { identifier: id })))
      .then((items) => { if (!cancelled) setAttachedDetails(items); })
      .catch(report);
    return () => { cancelled = true; };
  }, [attached.join(","), data.snapshot]);
  useEffect(() => {
    const timer = setInterval(
      () => {
        if (
          document.visibilityState === "visible" &&
          !detailRef.current &&
          !busy &&
          surface === "workspace"
        )
          refresh();
      },
      Math.max(15, settings.refreshSeconds || 30) * 1000,
    );
    const focus = () => {
      if (!detailRef.current && surface === "workspace") refresh();
    };
    window.addEventListener("focus", focus);
    return () => {
      clearInterval(timer);
      window.removeEventListener("focus", focus);
    };
  }, [settings.refreshSeconds, busy, surface]);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 5000);
    return () => clearTimeout(t);
  }, [toast]);
  useEffect(() => {
    const theme = settings.theme && settings.theme !== "system" ? settings.theme : hostTheme;
    document.documentElement.dataset.theme = theme;
    // Native date/time controls must follow the app override as well as host CSS.
    document.documentElement.style.colorScheme = theme;
  }, [settings.theme, hostTheme]);
  const selected = (surface === "selection" ? attachedDetails : data.items)
    .filter((i: D) => selection.includes(i.id));
  const change = async (tool: string, args: D, item?: D, canUndo = false) => {
    const response = await mutate(tool, args, item?.revision);
    const updated =
      canUndo && item && !item.recurrence
        ? await call("workspace_detail", { identifier: item.id }).catch(
            () => null,
          )
        : null;
    if (response.status === "partial" || response.status === "uncertain") {
      setError(
        response.message ||
          "Part of this change needs attention. Refresh before trying again.",
      );
      setUndo(null);
    } else if (canUndo && item && updated && !item.recurrence) {
      setUndo({
        revision: updated.revision,
        tool,
        args: {
          ...args,
          ...(tool === "set_completion"
            ? { completed: item.completed }
            : tool === "set_flagged"
              ? { flagged: item.flagged }
              : {}),
        },
      });
    } else setUndo(null);
    await refresh();
    if (item && detailRef.current?.id === item.id) {
      const fresh = await call("workspace_detail", {
        identifier: item!.id,
      }).catch(() => null);
      setDetail(fresh);
    }
    return response;
  };
  const complete = (item: D) =>
    run(() =>
      change(
        "set_completion",
        { reminder_id: item.id, completed: !item.completed },
        item,
        true,
      ),
    );
  const flag = (item: D) =>
    run(() =>
      change(
        "set_flagged",
        { reminder_id: item.id, flagged: !item.flagged },
        item,
        true,
      ),
    );
  const openQuickAdd = (defaults: D = {}) => {
    const current = data.lists.find((i: D) => i.id === query.listId);
    const preferred = data.lists.find((i: D) => String(i.id) === String(settings.defaultList) || i.title === settings.defaultList);
    const seed: D = {title: "", ...(current && !current.isGroup ? {list_id: current.id} : preferred ? {list_id: preferred.id} : settings.defaultList ? {list: settings.defaultList} : {}), ...(query.view === "today" ? {due: today()} : {}), ...(query.view === "flagged" ? {flagged: true} : {})};
    setQuickDraft(old => ({...(old || seed), ...defaults}));
    setError(""); setModal(null); setMenu(false); setQuickOpen(true);
  };
  const add = (another: boolean) => run(async () => {
    if (!quickDraft?.title?.trim()) return;
    const args = Object.fromEntries(Object.entries({...quickDraft, title: quickDraft.title.trim()}).filter(([,v]) => v !== undefined && v !== ""));
    // Once a reminder exists, clear its draft before refresh so a read failure cannot create it twice.
    const response = await mutate("create_reminder", args);
    if (response.id || response.status === "created") {
      setQuickDraft(another ? {...quickDraft, title: "", notes: ""} : null);
      setQuickOpen(another);
      setToast("Reminder added");
      if (response.warnings?.length) setError(response.warnings.join(" · "));
    }
    if (response.status === "partial" || response.status === "uncertain") {
      if (!response.id) setQuickOpen(false);
      setError(response.message || "Creation needs attention. Refresh before trying again.");
    }
    await refresh();
  });
  const bulk = (tool: string, args: D) =>
    run(async () => {
      let count = 0;
      for (const item of selected) {
        try {
          await mutate(tool, { reminder_id: item.id, ...args }, item.revision);
          count++;
        } catch (err) {
          await refresh();
          throw new Error(
            `${count} of ${selected.length} changed. ${String((err as Error).message)}`,
          );
        }
      }
      setSelection([]);
      setToast(`${count} reminders updated`);
      await refresh();
    });
  const undoChange = () =>
    run(async () => {
      if (!undo || busy) return;
      await mutate(undo.tool, undo.args, undo.revision);
      setUndo(null);
      await refresh();
      if (detailRef.current) await openDetail(detailRef.current);
    });
  const select = (item: D, event: React.MouseEvent) => {
    selectionFocus.current = item.id;
    if (event.shiftKey)
      setSelection(selectionRange(navigable, selectionAnchor.current, item.id));
    else if (event.metaKey || event.ctrlKey) {
      selectionAnchor.current = item.id;
      setSelection((old) =>
        old.includes(item.id)
          ? old.filter((id) => id !== item.id)
          : [...old, item.id],
      );
    } else {
      selectionAnchor.current = item.id;
      setSelection([item.id]);
      openDetail(item);
    }
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (
        (e.target as HTMLElement).closest("[role=menu],[role=dialog]") &&
        e.key !== "Escape"
      )
        return;
      const editing = (e.target as HTMLElement).matches(
        "input,textarea,select,[contenteditable]",
      );
      if (e.defaultPrevented) return;
      if (!editing && (e.target as HTMLElement).closest("button,a") && [" ", "Enter", "ArrowDown", "ArrowUp"].includes(e.key)) return;
      if (
        (e.metaKey || e.ctrlKey) &&
        e.key.toLowerCase() === "z" &&
        !editing &&
        undo
      ) {
        e.preventDefault();
        undoChange();
      } else if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setModal({ kind: "commands" });
      } else if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "Escape") {
        if (contextMenu) setContextMenu(null);
        else if (modal) setModal(null);
        else if (detail) setDetail(null);
        else setSelection([]);
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        if (e.shiftKey) openAction("create_list");
        else openQuickAdd();
      } else if (
        !editing &&
        e.shiftKey &&
        e.key === "F10" &&
        selected.length === 1
      ) {
        e.preventDefault();
        setContextMenu({ x: 300, y: 180, items: reminderActions(selected[0]) });
      } else if (
        !editing &&
        (e.key === "Backspace" || e.key === "Delete") &&
        selected.length === 1
      ) {
        e.preventDefault();
        setModal({ kind: "delete", item: selected[0] });
      } else if (!editing && e.key === "n") {
        e.preventDefault();
        openQuickAdd();
      } else if (!editing && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        e.preventDefault();
        const index = navigable.findIndex(
          (i: D) =>
            i.id ===
            (selectionFocus.current ?? selection[selection.length - 1]),
        );
        const row =
          navigable[
            Math.max(
              0,
              Math.min(
                navigable.length - 1,
                index + (e.key === "ArrowDown" ? 1 : -1),
              ),
            )
          ];
        if (row) {
          selectionFocus.current = row.id;
          if (e.shiftKey)
            setSelection(
              selectionRange(navigable, selectionAnchor.current, row.id),
            );
          else {
            selectionAnchor.current = row.id;
            setSelection([row.id]);
          }
          document
            .querySelector<HTMLElement>(`[data-reminder-id="${row.id}"]`)
            ?.scrollIntoView({ block: "nearest" });
        }
      } else if (!editing && e.key === " " && selected.length === 1) {
        e.preventDefault();
        complete(selected[0]);
      } else if (!editing && e.key === "Enter" && selected.length === 1)
        openDetail(selected[0]);
      else if ((e.metaKey || e.ctrlKey) && e.key === "a" && !editing) {
        e.preventDefault();
        setSelection(navigable.map((i: D) => i.id));
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [
    data,
    query,
    selection,
    settings,
    collapsed,
    collapsedSections,
    contextMenu,
    modal,
    detail,
    undo,
    busy,
    surface,
    attached,
  ]);
  const lists = (data.lists || []).map((list: D) => ({
      ...list, badge: { ...list.badge,
        image: symbols[list.badge?.symbol || "default"] || list.badge?.image },
    })),
    currentList = lists.find((l: D) => l.id === query.listId),
    heading =
      surface === "selection"
        ? "Selected Reminders"
        : currentList?.title ||
          data.smartLists?.find((l: D) => l.id === query.smartId)?.name ||
          ({ assigned: "Assigned to Me", deleted: "Recently Deleted" } as Record<string, string>)[query.view] ||
          titleCase(query.view || "Reminders");
  const smartList = data.smartLists?.find((l: D) => l.id === query.smartId);
  const sidebarEntries = sidebarLists(lists, data.smartLists || []).map((list: D) => ({
    ...list, badge: {...list.badge, image: symbols[list.badge?.symbol || "default"] || list.badge?.image},
  }));
  const sidebarSiblings = (scope: string) => orderSidebarItems(
    scope === "pinned" ? pinnedSidebarLists(lists, data.smartLists || []) : sidebarEntries.filter(list => sidebarScope(list, lists) === scope),
    sidebarOrder?.[scope],
  );
  const saveSidebarOrder = (scope: string, order: string[]) => run(async () => {
    const value = await call("update_sidebar_order", {scope, order});
    setSidebarOrder(value.orders);
  });
  const sidebarActions = (list: D): Action[] => {
    const scope = sidebarScope(list, lists), siblings = sidebarSiblings(scope), key = sidebarKey(list), index = siblings.findIndex(item => sidebarKey(item) === key);
    return [
      {label: "Move Up", disabled: busy || sidebarOrder === null || index <= 0, run: () => saveSidebarOrder(scope, moveSidebarItem(siblings, key, -1))},
      {label: "Move Down", disabled: busy || sidebarOrder === null || index < 0 || index === siblings.length - 1, run: () => saveSidebarOrder(scope, moveSidebarItem(siblings, key, 1))},
      {label: "Reset Order", disabled: busy || !sidebarOrder?.[scope]?.length, run: () => saveSidebarOrder(scope, [])},
      ...(list.sidebarKind === "smart" ? smartListActions(list) : listActions(list)),
    ];
  };
  const openSidebarList = (list: D) => navigate(list.sidebarKind === "smart" ? {view: "smart", smartId: list.id} : {view: "list", listId: list.id});
  const sidebarChosen = (list: D) => list.sidebarKind === "smart" ? query.smartId === list.id : query.listId === list.id;
  const activeColor = currentList
    ? colorFor(currentList, lists.indexOf(currentList))
    : smartList
      ? colorFor(smartList)
      : VIEW_COLORS[query.view] || VIEW_COLORS.today;
  // Each row's check circle takes its own list's color, as in Reminders.
  const rowColor = (item: D) => {
    const list = lists.find((l: D) => l.id === item.listId);
    return list ? colorFor(list, lists.indexOf(list)) : activeColor;
  };
  const visible = arrangeReminders(
    surface === "selection"
      ? attachedDetails.filter((i: D) => attached.includes(i.id))
      : data.items,
    collapsed,
  );
  const sectionKey = (item: D) => query.listId && item.groupSection ? item.groupSection : query.view === "today" ? todaySection(item, today()) : query.view === "scheduled" || layout === "calendar" ? ((item.displayDate || item.dueDate)?.slice(0, 10) || "No date") : "Reminders";
  const navigable = layout === "list" ? visible.filter(item => !collapsedSections.includes(sectionKey(item))) : visible;
  const groups: Record<string, D[]> = {};
  if (query.listId && layout !== "calendar" && (!loading || visible.length))
    for (const section of data.sections || [])
      if (section.listId === query.listId) groups[section.title] = [];
  for (const item of visible) {
    const key = sectionKey(item);
    (groups[key] ||= []).push(item);
  }
  if (query.listId && (!loading || visible.length))
    for (const section of data.sections || [])
      if (section.listId === query.listId) groups[section.title] ||= [];
  const openAction = (name: string, defaults: D = {}) => {
    setError("");
    if (name === "create_reminder") {openQuickAdd(defaults);return;}
    if (name === "manage_smart_list_create" || name === "manage_smart_list_edit") {
      const item = name.endsWith("edit") ? data.smartLists?.find((l: D) => l.id === defaults.smart_list_id) : null;
      if (name.endsWith("edit") && !item) {setError("Choose a smart list from the sidebar to edit.");return;}
      setModal({kind:"smart",item});return;
    }
    const tool = catalog.find((t) => t.name === name);
    if (tool) setModal({ kind: "action", tool, defaults });
    else setError("This action is not available yet.");
  };
  const batchItems = (items: D[], tool: string, args: D | ((item: D) => D)) =>
    run(async () => {
      let done = 0;
      try {
        for (const item of items) {
          await mutate(
            tool,
            {
              reminder_id: item.id,
              ...(typeof args === "function" ? args(item) : args),
            },
            item.revision,
          );
          done++;
        }
      } catch (err) {
        throw new Error(
          `${done} of ${items.length} changed. ${(err as Error).message}`,
        );
      } finally {
        await refresh();
      }
      setToast(`${done} reminder${done === 1 ? "" : "s"} updated`);
    });
  const reminderActions = (item: D): Action[] => {
    if (query.view === "deleted")
      return [
        {
          label: "Restore reminder…",
          icon: <Undo2 size={15} />,
          run: () =>
            openAction("restore_reminder", {
              reminder_id: item.restoreId || item.id,
              private: true,
            }),
        },
      ];
    const items: D[] =
      selection.includes(item.id) && selected.length > 1 ? selected : [item];
    return [
      {
        label: "Show details",
        icon: <PanelRight size={15} />,
        shortcut: "↵",
        run: () => openDetail(item),
      },
      {
        label: items.every((i) => i.completed) ? "Mark incomplete" : "Complete",
        icon: <Check size={15} />,
        shortcut: "Space",
        run: () =>
          items.length === 1
            ? complete(item)
            : batchItems(items, "set_completion", {
                completed: !items.every((i) => i.completed),
              }),
      },
      {
        label: items.every((i) => i.flagged) ? "Unflag" : "Flag",
        icon: <Flag size={15} />,
        run: () =>
          items.length === 1
            ? flag(item)
            : batchItems(items, "set_flagged", {
                flagged: !items.every((i) => i.flagged),
              }),
      },
      {
        label: "Schedule",
        icon: <CalendarDays size={15} />,
        children: [
          ["Today", "today"],
          ["Tomorrow", "tomorrow"],
          ["Next week", "in 7 days"],
          ["Remove date", "clear"],
        ].map(([label, due]) => ({
          label,
          run: () => batchItems(items, "update_reminder", { due }),
        })),
      },
      {
        label: "Priority",
        icon: <ArrowUpDown size={15} />,
        children: ["high", "medium", "low", "none"].map((priority) => ({
          label: titleCase(priority),
          run: () => batchItems(items, "update_reminder", { priority }),
        })),
      },
      {
        label: "Move to List",
        icon: <Folder size={15} />,
        children: lists
          .filter((l: D) => !l.isGroup && l.id !== item.listId)
          .map((l: D) => ({
            label: l.title,
            icon: <ListBadge list={l} color={colorFor(l)} />,
            run: () => batchItems(items, "update_reminder", { list_id: l.id }),
          })),
      },
      {
        label: "Move to Section",
        disabled: !settings.advancedFeatures,
        icon: <Columns3 size={15} />,
        children: [
          {
            label: "None",
            run: () =>
              batchItems(items, "update_reminder", {
                no_section: true,
                private: true,
              }),
          },
          ...data.sections
            .filter((section: D) => section.listId === item.listId)
            .map((section: D) => ({
              label: section.title,
              run: () =>
                batchItems(items, "update_reminder", {
                  section_id: section.objectUUID,
                  private: true,
                }),
            })),
        ],
      },
      {
        label: "Attach to conversation",
        icon: <Paperclip size={15} />,
        run: () =>
          run(async () => {
            await attach(items);
            setAttached(items.map((i) => i.id));
            setToast("Attached to conversation");
          }),
      },
      {
        label: "Ask ChatGPT…",
        icon: <MessageSquare size={15} />,
        run: () => setModal({ kind: "conversation", items }),
      },
      {
        label: "Copy reminder link",
        icon: <Link2 size={15} />,
        run: () =>
          run(async () => {
            await navigator.clipboard.writeText(
              `codex://plugins/remctl@remctl-local/app/open_workspace?path=${encodeURIComponent("/reminder/" + item.objectUUID)}`,
            );
            setToast("Link copied");
          }),
      },
      {
        label: "Open in Reminders",
        icon: <ArrowUpRight size={15} />,
        run: () => run(() => safeLink(item.deepLink)),
      },
      {
        label: "Delete reminder…",
        icon: <Trash2 size={15} />,
        danger: true,
        disabled: items.length > 1,
        run: () => setModal({ kind: "delete", item }),
      },
    ];
  };
  const togglePin = (list: D, smart = false) => run(async () => {
    const response = await change(list.pinned ? "manage_list_unpin" : "manage_list_pin", {...(smart ? {smart_list_id: list.id} : {list_id: list.id}), private: true});
    if (response.status !== "partial" && response.status !== "uncertain") setToast(list.pinned ? "List unpinned" : "List pinned");
  });
  const smartListActions = (list: D): Action[] => [
    {label: list.pinned ? "Unpin smart list" : "Pin smart list", disabled: !settings.advancedFeatures, run: () => togglePin(list, true)},
    {label: "Edit smart list…", run: () => openAction("manage_smart_list_edit", {smart_list_id: list.id, private: true})},
    {label: "Delete smart list…", danger: true, run: () => openAction("manage_smart_list_delete", {smart_list_id: list.id, private: true})},
  ];
  const pinButton = (list: D, smart = false) => !list.isGroup && <button type="button" className={"list-pin " + (list.pinned ? "pinned" : "")} aria-label={`${list.pinned ? "Unpin" : "Pin"} ${list.title || list.name}`} aria-pressed={Boolean(list.pinned)} title={settings.advancedFeatures ? (list.pinned ? "Unpin list" : "Pin list") : "Enable Advanced Reminders features to pin lists"} disabled={busy || !settings.advancedFeatures} onClick={() => togglePin(list, smart)}><Pin size={12} fill={list.pinned ? "currentColor" : "none"}/></button>;
  const listActions = (list: D): Action[] =>
    list.isGroup
      ? [
          {
            label: "Edit group…",
            run: () =>
              openAction("manage_group_edit", {
                group_id: list.id,
                private: true,
              }),
          },
          {
            label: "New list in group…",
            run: () =>
              openAction("create_list", { group_id: list.id, private: true }),
          },
        ]
      : [
          {
            label: "Rename…",
            run: () => openAction("manage_list_rename", { list_id: list.id }),
          },
          {
            label: "Color & icon…",
            disabled: !settings.advancedFeatures,
            run: () => setModal({ kind: "appearance", list }),
          },
          {
            label: list.pinned ? "Unpin list" : "Pin list",
            icon: <Pin size={15} />,
            disabled: !settings.advancedFeatures,
            run: () => togglePin(list),
          },
          {
            label: "New section…",
            icon: <Columns3 size={15} />,
            disabled: !settings.advancedFeatures,
            run: () =>
              openAction("manage_section_create", {
                list_id: list.id,
                private: true,
              }),
          },
          {
            label: "Save as template…",
            icon: <Copy size={15} />,
            disabled: !settings.advancedFeatures,
            run: () =>
              openAction("manage_template_create", {
                from_list_id: list.id,
                private: true,
              }),
          },
          {
            label: "Move to Group",
            icon: <Folder size={15} />,
            disabled: !settings.advancedFeatures,
            children: lists
              .filter((l: D) => l.isGroup && l.id !== list.parentListId)
              .map((l: D) => ({
                label: l.title,
                run: () =>
                  run(() =>
                    change("manage_group_edit", {
                      group_id: l.id,
                      add_list_id: [list.id],
                      private: true,
                    }),
                  ),
              })),
          },
          {
            label: "Export list…",
            icon: <Download size={15} />,
            run: () =>
              run(async () => {
                const file = await call("export_remctl_file", {
                  listId: list.id,
                });
                if (extensions.files) await extensions.files.open(file.path);
                else setToast("Exported to Downloads");
              }),
          },
        ];
  const showContext = (e: React.MouseEvent, items: Action[]) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({ x: e.clientX, y: e.clientY, items });
  };
  const startDrag = (e: React.DragEvent, item: D) => {
    const items: D[] = selection.includes(item.id) ? selected : [item];
    draggingIds.current = items.map((i) => i.id);
    setToast(`Moving ${items.length === 1 ? item.title : `${items.length} reminders`}`);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/remctl-id", String(item.id));
    e.dataTransfer.setData(
      "application/x-remctl-ids",
      JSON.stringify(items.map((i) => i.id)),
    );
    e.dataTransfer.setData("text/plain", items.map((i) => i.title).join("\n"));
  };
  const draggedItems = (e: React.DragEvent): D[] => {
    try {
      // Keep internal drags intact when the Mac web view strips custom MIME data.
      const payload = e.dataTransfer.getData("application/x-remctl-ids");
      const ids = payload ? JSON.parse(payload) : draggingIds.current;
      return data.items.filter((i: D) => ids.includes(i.id));
    } catch {
      return [];
    }
  };
  const dropIntoList = (e: React.DragEvent, list: D) => {
    e.preventDefault();
    e.stopPropagation();
    const movedList = Number(e.dataTransfer.getData("text/remctl-list"));
    if (movedList && list.isGroup && settings.advancedFeatures) {
      run(() =>
        change("manage_group_edit", {
          group_id: list.id,
          add_list_id: [movedList],
          private: true,
        }),
      );
      return;
    }
    if (!list.isGroup) {
      const items = draggedItems(e);
      if (items.length)
        batchItems(items, "update_reminder", { list_id: list.id });
    }
  };
  const drop = (e: React.DragEvent, target: D) => {
    e.currentTarget.classList.remove("drag-target");
    if (e.dataTransfer.files.length) {
      e.preventDefault();
      e.stopPropagation();
      const files = Array.from(e.dataTransfer.files);
      if (!settings.advancedFeatures) {
        setError("Enable Advanced Reminders features to attach images.");
        return;
      }
      run(async () => {
        await attachImages(target, files);
        await refresh();
        await openDetail(target);
      });
      return;
    }
    const link = e.dataTransfer
      .getData("text/uri-list")
      .split("\n")
      .find((v) => /^https?:\/\//i.test(v));
    if (link && !e.dataTransfer.types.includes("application/x-remctl-ids")) {
      e.preventDefault();
      e.stopPropagation();
      run(() =>
        change(
          "update_reminder",
          {
            reminder_id: target.id,
            url: link,
            private: !!settings.advancedFeatures,
          },
          target,
        ),
      );
      return;
    }
    if (
      layout === "columns" &&
      draggedItems(e).some((i) => i.section !== target.section)
    )
      return;
    e.preventDefault();
    e.stopPropagation();
    if (!settings.advancedFeatures || query.view !== "list") return;
    const items = draggedItems(e).filter((i) => i.id !== target.id);
    if (items.length)
      run(async () => {
        for (const item of items)
          await mutate(
            "manage_reminder_move",
            { id: item.id, before: target.id, private: true },
            item.revision,
          );
        await refresh();
      });
  };
  const internalDrag = useInternalDrag((payload, target) => {
    const kind = target.dataset.dropKind;
    const id = Number(target.dataset.dropId);
    if (payload.kind === "list") {
      if (kind === "group" && settings.advancedFeatures)
        run(() => change("manage_group_edit", {group_id: id, add_list_id: [payload.id], private: true}));
      return;
    }
    const items = data.items.filter((item: D) => payload.ids.includes(item.id));
    if (!items.length) return;
    if (kind === "date") batchItems(items, "update_reminder", (item) => ({due: rescheduledDue(item, target.dataset.dropDate!)}));
    else if (kind === "list") batchItems(items, "update_reminder", {list_id: id});
    else if (kind === "section" && settings.advancedFeatures && query.listId)
      batchItems(items, "update_reminder", {...(target.dataset.dropSection ? {section_id: target.dataset.dropSection} : {no_section: true}), private: true});
    else if (kind === "reminder" && settings.advancedFeatures && query.view === "list") {
      const anchor = data.items.find((item: D) => item.id === id);
      if (!anchor || payload.ids.includes(id)) return;
      if (layout === "columns" && items.some((item: D) => item.section !== anchor.section)) {
        const section = data.sections.find((value: D) => value.title === anchor.section && value.listId === query.listId);
        batchItems(items, "update_reminder", {...(section ? {section_id: section.objectUUID} : {no_section: true}), private: true});
      } else run(async () => {
        for (const item of items) await mutate("manage_reminder_move", {id: item.id, before: id, private: true}, item.revision);
        await refresh();
      });
    }
  });
  const beginReminderDrag = (e: React.PointerEvent, item: D) => {
    if (query.view === "deleted") return;
    const items: D[] = selection.includes(item.id) ? selected : [item];
    internalDrag.begin(e, {kind: "reminders", ids: items.map((i) => i.id), label: items.length === 1 ? item.title : `${items.length} reminders`});
  };
  const paletteActions: Action[] = [
    {
      label: "New reminder",
      icon: <Plus size={17} />,
      shortcut: "⌘ N",
      run: () => {
        setModal(null);
        openQuickAdd();
      },
    },
    ...(selected.length === 1
      ? reminderActions(selected[0])
          .filter((a) => a.run)
          .map((a) => ({
            ...a,
            run: () => {
              setModal(null);
              a.run?.();
            },
          }))
      : []),
    {label:"Go to Urgent",run:()=>{navigate({view:"urgent"});setModal(null);}},
    {label:"Go to Overdue",run:()=>{navigate({view:"overdue"});setModal(null);}},
    ...VIEWS.map(([id, label, Icon]) => ({
      label: "Go to " + label,
      icon: <Icon size={17} />,
      run: () => {
        navigate({ view: id });
        setModal(null);
      },
    })),
    ...lists.map((l: D) => ({
      label: l.title,
      keywords: "list " + l.title,
      icon: <ListBadge list={l} color={colorFor(l)} />,
      run: () => {
        navigate({ view: "list", listId: l.id });
        setModal(null);
      },
    })),
    ...["list", "columns", "calendar"].map((value) => ({
      label: titleCase(value) + " layout",
      run: () => {
        chooseLayout(value);
        setModal(null);
      },
    })),
    {
      label: "Settings",
      icon: <Settings2 size={17} />,
      run: () => setModal({ kind: "settings" }),
    },
    ...catalog.map((t) => ({
      label: t.title,
      keywords: t.name,
      run: () =>
        openAction(t.name, {
          ...(t.inputSchema.properties?.private ? { private: true } : {}),
          ...(t.inputSchema.properties?.list_id && query.listId
            ? { list_id: query.listId }
            : {}),
          ...(t.inputSchema.properties?.reminder_id && selected.length === 1
            ? { reminder_id: selected[0].id }
            : {}),
        }),
    })),
    ...data.items.map((i: D) => ({
      label: i.title,
      keywords: "reminder " + i.list,
      icon: <CheckCircle2 size={17} />,
      run: () => {
        setModal(null);
        openDetail(i);
      },
    })),
  ];
  const sectionActions = (name: string): Action[] => {
    const section = data.sections.find(
      (s: D) => s.title === name && s.listId === query.listId,
    );
    if (!section) return [];
    const defaults = {
      list_id: query.listId,
      section_id: section.objectUUID,
      private: true,
    };
    return [
      {
        label: "Add reminder…",
        icon: <Plus size={15} />,
        run: () => openAction("create_reminder", defaults),
      },
      {
        label: "Rename section…",
        run: () => openAction("manage_section_rename", defaults),
      },
      {
        label: "Delete section…",
        danger: true,
        run: () => openAction("manage_section_delete", defaults),
      },
    ];
  };
  const row = (item: D) => (
    <div
      key={item.id}
      className={
        "task-row " +
        (item.depth ? "subtask " : "") +
        (selection.includes(item.id) ? "selected " : "") +
        (item.completed ? "completed " : "") +
        (detail?.id === item.id ? "inspected" : "")
      }
      role="option"
      tabIndex={item.id === (selection[0] ?? navigable[0]?.id) ? 0 : -1}
      data-reminder-id={item.id}
      data-drop-kind="reminder"
      data-drop-id={item.id}
      style={{ "--depth": item.depth || 0, "--row-color": rowColor(item) } as React.CSSProperties}
      aria-selected={selection.includes(item.id)}
      draggable={false}
      onPointerDown={(e) => beginReminderDrag(e, item)}
      onDragStart={(e) => startDrag(e, item)}
      onContextMenu={(e) => showContext(e, reminderActions(item))}
      onDragOver={(e) => {
        e.preventDefault();
        e.currentTarget.classList.add("drag-target");
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node))
          e.currentTarget.classList.remove("drag-target");
      }}
      onDrop={(e) => drop(e, item)}
      onClick={(e) => { e.currentTarget.focus(); select(item, e); }}
    >
      <button
        className={"check " + (item.completed ? "checked" : "")}
        aria-label={
          (item.completed ? "Mark incomplete: " : "Complete: ") + item.title
        }
        onClick={(e) => {
          e.stopPropagation();
          if (query.view !== "deleted") complete(item);
        }}
        disabled={busy || query.view === "deleted"}
      >
        {item.completed && <Check size={12} />}
      </button>
      <div className="task-copy">
        <span className="task-title">
          {item.priority === "high" && <b className="priority">!!! </b>}
          {item.title}
        </span>
        <NotePreview item={item} run={run}/>
        <div className="task-meta">
          {!currentList && item.list && (
            <span className="list-name">{item.list}</span>
          )}
          {item.dueDate && (
            <span
              className={
                item.dueDate.slice(0, 10) < today() && !item.completed
                  ? "overdue"
                  : ""
              }
            >
              {dateText(item)}
            </span>
          )}
          {item.tags?.map((t: string) => (
            <button
              key={t}
              className="tag"
              onClick={(e) => {
                e.stopPropagation();
                const next = { ...query, query: "#" + t };
                setSearch("#" + t);
                setQuery(next);
                refresh(next);
              }}
            >
              #{t}
            </button>
          ))}
          {item.subtaskCount > 0 && (
            <button
              className="subtask-count"
              aria-expanded={!collapsed.includes(item.id)}
              aria-label={(collapsed.includes(item.id) ? "Show " : "Hide ") + item.subtaskCount + " subtasks: " + item.title}
              onClick={(e) => {
                e.stopPropagation();
                setCollapsed((old) => old.includes(item.id) ? old.filter((id) => id !== item.id) : [...old, item.id]);
              }}
            >
              <Layers size={11} />
              {item.subtaskCount} {item.subtaskCount === 1 ? "subtask" : "subtasks"}
              {collapsed.includes(item.id) ? <ChevronRight size={11} /> : <ChevronDown size={11} />}
            </button>
          )}
          {item.attachments?.length > 0 && <Paperclip size={12} />}{" "}
          {item.recurrence && <span title={recurrenceText(item.recurrence)}><Repeat2 size={12} />{titleCase(item.recurrence.frequency || "Repeats")}</span>}
        </div>
        <RichLinks item={item} run={run}/>
        {!!item.attachments?.length && <div className="task-inline-attachments" onClick={e => e.stopPropagation()}><AttachmentGallery item={item} run={run}/></div>}
      </div>
      {item.assignment?.assignee && (
        <span className="avatar" title={item.assignment.assignee.name}>
          {item.assignment.assignee.name?.slice(0, 1)}
        </span>
      )}
      {item.flagged && <Flag className="flag" size={14} fill="currentColor" />}
      <button className="row-more" aria-label={"Details: " + item.title}>
        <Info size={15} />
      </button>
    </div>
  );
  const days = (() => {
    const first = new Date(month.getFullYear(), month.getMonth(), 1);
    const offset =
      (first.getDay() + (settings.weekStartsOn === "sunday" ? 0 : 6)) % 7;
    return Array.from(
      { length: 42 },
      (_, i) => new Date(first.getFullYear(), first.getMonth(), 1 - offset + i),
    );
  })();
  return (
    <div
      className={
        "workspace " +
        settings.density +
        " " +
        (sidebar ? "" : "no-sidebar") +
        " " +
        (surface === "inline" ? "inline" : "")
      }
      style={{ "--accent": activeColor } as React.CSSProperties}
      onDragEnd={() => {
        draggingIds.current = [];
        document.querySelectorAll(".drag-target").forEach((element) => element.classList.remove("drag-target"));
      }}
    >
      {internalDrag.preview && <div className="drag-preview" style={{left: internalDrag.preview.x + 14, top: internalDrag.preview.y + 14}}><Layers size={14} />{internalDrag.preview.label}</div>}
      {sidebar && surface !== "file" && surface !== "inline" && (
        <aside className="sidebar">
          <div className="sidebar-panel">
          <div className="brand">
            <img src={icon} alt="" />
            <span>RemCTL</span>
            <button className="sidebar-close" aria-label="Close sidebar" onClick={() => setSidebar(false)}><X size={15}/></button>
            <IconButton
              label="Command palette"
              onClick={() => setModal({ kind: "commands" })}
            >
              <Command size={14} />
            </IconButton>
          </div>
          <div className="sidebar-scroll">
          <div className="smart-grid" aria-label="Pinned lists and smart lists">
            {VIEWS.filter(([id]) => systemListVisible(data.smartLists || [], id)).map(([id, label, Icon, color]) => (
              <button
                key={id}
                className={
                  "smart-card " +
                  color +
                  " " +
                  (query.view === id && !query.listId && !query.smartId ? "chosen" : "")
                }
                onClick={() => navigate({ view: id })}
              >
                <span className="smart-top">
                  <span className="smart-symbol">
                    <Icon size={17} />
                  </span>
                  {id !== "completed" && <strong>{data.counts?.[id] ?? "—"}</strong>}
                </span>
                <span className="smart-title">{label}</span>
              </button>
            ))}
            {sidebarSiblings("pinned").map((list: D) => {
              const smart = list.sidebarKind === "smart", title = list.title || list.name;
              const selected = smart ? query.smartId === list.id : query.listId === list.id;
              return <div className="pinned-tile" key={`${list.sidebarKind}-${list.id}`} style={{"--card": colorFor(list)} as React.CSSProperties}>
                <button className={"smart-card " + (selected ? "chosen" : "")} aria-label={`${title}, pinned${smart ? " smart" : ""} list`} title={title}
                  onClick={() => navigate(smart ? {view: "smart", smartId: list.id} : {view: "list", listId: list.id})}
                  onContextMenu={(e) => showContext(e, sidebarActions(list))}
                  onDragOver={smart ? undefined : (e) => e.preventDefault()}
                  onDrop={smart ? undefined : (e) => dropIntoList(e, list)}
                  data-drop-kind={smart ? undefined : "list"} data-drop-id={smart ? undefined : list.id}
                  onPointerDown={(e) => {if (!smart && settings.advancedFeatures) internalDrag.begin(e, {kind: "list", id: list.id, label: title});}}>
                  <span className="smart-top"><ListBadge list={{...list, badge: {...list.badge, image: symbols[list.badge?.symbol || "default"] || list.badge?.image}}} color={colorFor(list)}/><strong>{list.count ?? (smart ? "—" : 0)}</strong></span>
                  <span className="smart-title">{title}</span>
                </button>
                {pinButton(list, smart)}
              </div>;
            })}
          </div>
          <nav>
            {attached.length > 0 && (
              <button className={"nav-row " + (surface === "selection" ? "chosen" : "")} onClick={() => {
                setSurface("selection"); setDetail(null); setSelection([]);
              }}>
                <span className="nav-icon"><Paperclip size={15} /></span><span>Selected Reminders</span><small>{attached.length}</small>
              </button>
            )}
            <button
              className={
                "nav-row " + (query.view === "assigned" ? "chosen" : "")
              }
              onClick={() => navigate({ view: "assigned" })}
            >
              <span className="nav-icon"><UserRound size={15} /></span>
              <span>Assigned to Me</span>
            </button>
            {VIEWS.filter(([id]) => !systemListVisible(data.smartLists || [], id)).map(([id, label, Icon]) => <button key={id} className={"nav-row " + (query.view === id ? "chosen" : "")} onClick={() => navigate({view: id})}><span className="nav-icon"><Icon size={15}/></span><span>{label}</span></button>)}
            <div className="nav-label">
              <span>My Lists</span>
              {settings.advancedFeatures && <IconButton label="New smart list" onClick={() => openAction("manage_smart_list_create")}><Sparkles size={13}/></IconButton>}
              <IconButton
                label="New list"
                onClick={() => openAction("create_list")}
              >
                <Plus size={13} />
              </IconButton>
            </div>
            {sidebarSiblings("top")
              .map((list: D, index: number) => {
                const folded = collapsedGroups.includes(list.id);
                return (
                <React.Fragment key={`${list.sidebarKind}-${list.id}`}>
                  <div className={"sidebar-list-row " + (list.isGroup ? "group-row" : "")}>
                  {list.isGroup && (
                    <button
                      className="group-disclosure"
                      aria-label={(folded ? "Expand " : "Collapse ") + list.title}
                      aria-expanded={!folded}
                      onClick={() => setCollapsedGroups((old) => folded ? old.filter((id) => id !== list.id) : [...old, list.id])}
                    >
                      {folded ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                    </button>
                  )}
                  <button
                    className={
                      "nav-row " + (list.isGroup ? "group " : "") + (sidebarChosen(list) ? "chosen" : "")
                    }
                    onClick={() => openSidebarList(list)}
                    onContextMenu={(e) => showContext(e, sidebarActions(list))}
                    onDragOver={(e) => {if (list.sidebarKind !== "smart") e.preventDefault();}}
                    onDrop={(e) => {if (list.sidebarKind !== "smart") dropIntoList(e, list);}}
                    draggable={false}
                    data-drop-kind={list.sidebarKind === "smart" ? undefined : list.isGroup ? "group" : "list"}
                    data-drop-id={list.id}
                    onPointerDown={(e) => {if (settings.advancedFeatures && !list.isGroup && list.sidebarKind !== "smart") internalDrag.begin(e, {kind: "list", id: list.id, label: list.title});}}
                    onDragStart={(e) =>
                      e.dataTransfer.setData(
                        "text/remctl-list",
                        String(list.id),
                      )
                    }
                  >
                    <ListBadge list={list} color={colorFor(list, index)} />
                    <span>{list.title}</span>
                    <small>{list.isGroup ? "" : list.count || ""}</small>
                  </button>
                  {pinButton(list, list.sidebarKind === "smart")}
                  </div>
                  {list.isGroup && !folded &&
                    sidebarSiblings(`group:${list.objectUUID || list.id}`)
                      .map((child: D, j: number) => (
                        <div className="sidebar-list-row" key={`${child.sidebarKind}-${child.id}`}>
                        <button
                          onContextMenu={(e) =>
                            showContext(e, sidebarActions(child))
                          }
                          onDragOver={(e) => {if (child.sidebarKind !== "smart") e.preventDefault();}}
                          onDrop={(e) => {if (child.sidebarKind !== "smart") dropIntoList(e, child);}}
                          draggable={false}
                          data-drop-kind={child.sidebarKind === "smart" ? undefined : "list"}
                          data-drop-id={child.id}
                          onPointerDown={(e) => {if (settings.advancedFeatures && child.sidebarKind !== "smart") internalDrag.begin(e, {kind: "list", id: child.id, label: child.title});}}
                          onDragStart={(e) =>
                            e.dataTransfer.setData(
                              "text/remctl-list",
                              String(child.id),
                            )
                          }
                          className={
                            "nav-row child " +
                            (sidebarChosen(child) ? "chosen" : "")
                          }
                          onClick={() =>
                            openSidebarList(child)
                          }
                        >
                          <ListBadge list={child} color={colorFor(child, j)} />
                          <span>{child.title}</span>
                          <small>{child.count || ""}</small>
                        </button>
                        {pinButton(child, child.sidebarKind === "smart")}
                        </div>
                      ))}
                </React.Fragment>
              );})}
            <button
              className={"nav-row trash-row " + (query.view === "deleted" ? "chosen" : "")}
              onClick={() => navigate({ view: "deleted" })}
            >
              <span className="nav-icon"><Trash2 size={15} /></span>
              <span>Recently Deleted</span>
            </button>
          </nav>
          </div>
          <div className="sidebar-footer">
            <button className="add-list" onClick={() => openAction("create_list")}>
              <Plus size={15} /> Add List
            </button>
            <IconButton
              label="Settings"
              onClick={() => setModal({ kind: "settings" })}
            >
              <Settings2 size={15} />
            </IconButton>
          </div>
          </div>
        </aside>
      )}
      {sidebar && <button className="sidebar-backdrop" aria-label="Dismiss sidebar" onClick={() => setSidebar(false)} />}
      <main>
        {surface !== "file" && <header className="toolbar">
          <div className="toolbar-left">
            <IconButton
              label="Toggle sidebar"
              onClick={() => setSidebar(!sidebar)}
            >
              <PanelLeft size={16} />
            </IconButton>
          </div>
          <div className="toolbar-right">
            <div className="toolbar-group segmented" role="group" aria-label="Layout">
              {[
                ["list", List],
                ["columns", Columns3],
                ["calendar", CalendarDays],
              ].map(([id, Icon]: any) => (
                <IconButton
                  key={id}
                  label={titleCase(id) + " layout"}
                  active={layout === id}
                  onClick={() => chooseLayout(id)}
                >
                  <Icon size={15} />
                </IconButton>
              ))}
            </div>
            <div className="toolbar-group">
              <IconButton
                label="Refresh"
                disabled={loading}
                onClick={() => refresh()}
              >
                <RefreshCw size={14} className={loading ? "spinning" : ""} />
              </IconButton>
              <IconButton label="More actions" active={menu} onClick={() => setMenu(!menu)}>
                <MoreHorizontal size={17} />
              </IconButton>
            </div>
            <label className="toolbar-search">
              <Search size={14} />
              <input
                ref={searchRef}
                aria-label="Search reminders"
                placeholder="Search"
                value={search}
                onChange={(e) => {
                  const value = e.target.value;
                  setSearch(value);
                  setSurface("workspace");
                  const next = { view: "all", query: value };
                  setQuery(next);
                  refresh(next);
                }}
              />
              <kbd>⌘F</kbd>
            </label>
            <button
              className="new-button"
              aria-label="New Reminder"
              onClick={() => openQuickAdd()}
            >
              <Plus size={15} />
              <span>New Reminder</span>
            </button>
          </div>
        </header>}
        {menu && (
          <div className="action-menu">
            <button
              onClick={() => {
                setMenu(false);
                setModal({ kind: "commands" });
              }}
            >
              All actions <kbd>⌘ K</kbd>
            </button>
            <button
              onClick={() => {
                setMenu(false);
                run(async () => {
                  const value = await call(
                    "export_remctl_file",
                    { query },
                  );
                  if (extensions.files) await extensions.files.open(value.path);
                  else setToast("Saved to Downloads");
                });
              }}
            >
              Export RemCTL file
            </button>
            {["json","csv"].map(format=><button key={format} onClick={()=>{setMenu(false);run(async()=>{const value=await call("export_remctl_file",{query,format});if(extensions.files)await extensions.files.open(value.path);else setToast("Saved to Downloads");});}}>Export {format.toUpperCase()}</button>)}
            <button
              onClick={() => {
                setMenu(false);
                const next = {
                  ...query,
                  includeCompleted: !query.includeCompleted,
                };
                setQuery(next);
                refresh(next);
              }}
            >
              Show completed <span>{query.includeCompleted ? "✓" : ""}</span>
            </button>
            {currentList && (
              <button
                onClick={() => {
                  setMenu(false);
                  setModal({ kind: "appearance", list: currentList });
                }}
              >
                List appearance
              </button>
            )}
            <button
              onClick={() => {
                setMenu(false);
                setModal({ kind: "settings" });
              }}
            >
              Settings
            </button>
          </div>
        )}
        {error && (
          <div className="error-banner" role="alert">
            <AlertCircle size={16} />
            <span>{error}</span>
            <IconButton label="Dismiss error" onClick={() => setError("")}>
              <X size={14} />
            </IconButton>
          </div>
        )}
        {surface === "file" && file ? (
          <FileView file={file} run={run} report={report} />
        ) : (
          <div className="main-body">
            <div className={`collection layout-${layout}`}>
              <div className="collection-heading">
                <div className="heading-main">
                  <h1>{heading}</h1>
                  <span className="heading-count" aria-label="Reminder count">
                    {surface === "selection"
                      ? visible.length
                      : (data.total ?? (loading || readFailed ? "—" : visible.length))}
                  </span>
                </div>
                <div className="heading-sub">
                  <span className="heading-subtitle">
                    {query.view === "today"
                      ? new Date().toLocaleDateString(undefined, {
                          weekday: "long",
                          month: "long",
                          day: "numeric",
                        })
                      : surface === "selection"
                        ? "Shared with this conversation"
                        : currentList?.isGroceries
                          ? "Groceries"
                          : query.view === "completed"
                            ? "Nicely done."
                            : query.view === "deleted"
                              ? "Deleted reminders stay here for up to 30 days."
                              : query.query
                                ? `Results for “${query.query}”`
                                : ""}
                  </span>
                  <div className="heading-actions">
                    <Select
                      aria-label="Sort reminders"
                      value={query.sort || "manual"}
                      onChange={(e) => {
                        const next = { ...query, sort: e.target.value };
                        setQuery(next);
                        refresh(next);
                      }}
                    >
                      <option value="manual">Manual</option>
                      <option value="due">Due date</option>
                      <option value="priority">Priority</option>
                      <option value="title">Title</option>
                    </Select>
                    {currentList && (
                      <IconButton
                        label="New section"
                        onClick={() =>
                          openAction("manage_section_create", {
                            list_id: currentList.id,
                            private: true,
                          })
                        }
                      >
                        <Columns3 size={15} />
                      </IconButton>
                    )}
                  </div>
                </div>
              </div>
              {selection.length > 1 && (
                <div className="selection-bar">
                  <span>{selection.length} selected</span>
                  <button
                    onClick={() => bulk("set_completion", { completed: true })}
                  >
                    <Check size={14} />
                    Complete
                  </button>
                  <button
                    onClick={() => bulk("set_flagged", { flagged: true })}
                  >
                    <Flag size={13} />
                    Flag
                  </button>
                  <button
                    onClick={() => bulk("update_reminder", { due: "tomorrow" })}
                  >
                    <CalendarDays size={14} />
                    Tomorrow
                  </button>
                  <button
                    onClick={() =>
                      setModal({ kind: "bulkMove", items: selected })
                    }
                  >
                    Move
                  </button>
                  <button
                    disabled={!extensions.modelContext}
                    onClick={() =>
                      run(async () => {
                        await attach(selected);
                        setAttached(selection);
                        setToast("Added to conversation context");
                      })
                    }
                  >
                    <Paperclip size={14} />
                    Attach
                  </button>
                  <button
                    onClick={() =>
                      setModal({ kind: "conversation", items: selected })
                    }
                  >
                    <MessageSquare size={14} />
                  </button>
                  <IconButton
                    label="Clear selection"
                    onClick={() => setSelection([])}
                  >
                    <X size={14} />
                  </IconButton>
                </div>
              )}
              {layout === "calendar" ? (
                <div className="calendar">
                  <div className="calendar-heading">
                    <strong>
                      {month.toLocaleDateString(undefined, {
                        month: "long",
                        year: "numeric",
                      })}
                    </strong>
                    <div>
                      <IconButton
                        label="Previous month"
                        onClick={() =>
                          setMonth(
                            new Date(
                              month.getFullYear(),
                              month.getMonth() - 1,
                              1,
                            ),
                          )
                        }
                      >
                        <ChevronLeft size={16} />
                      </IconButton>
                      <button onClick={() => setMonth(new Date())}>
                        Today
                      </button>
                      <IconButton
                        label="Next month"
                        onClick={() =>
                          setMonth(
                            new Date(
                              month.getFullYear(),
                              month.getMonth() + 1,
                              1,
                            ),
                          )
                        }
                      >
                        <ChevronRight size={16} />
                      </IconButton>
                    </div>
                  </div>
                  <div className="calendar-grid">
                    {(settings.weekStartsOn === "sunday"
                      ? ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
                      : ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                    ).map((d) => (
                      <div key={d} className="day-label">
                        {d}
                      </div>
                    ))}
                    {days.map((day) => {
                      const date = day.toLocaleDateString("en-CA");
                      return (
                        <div
                          key={date}
                          data-drop-kind="date"
                          data-drop-date={date}
                          className={
                            "calendar-day " +
                            (day.getMonth() !== month.getMonth()
                              ? "outside"
                              : "")
                          }
                          onDragOver={(e) => e.preventDefault()}
                          onDrop={(e) => {
                            e.preventDefault();
                            const items = draggedItems(e);
                            if (items.length)
                              batchItems(items, "update_reminder", (item) => ({
                                due: rescheduledDue(item, date),
                              }));
                          }}
                          onDoubleClick={() =>
                            openAction("create_reminder", {
                              due: date,
                              ...(query.listId
                                ? { list_id: query.listId }
                                : {}),
                            })
                          }
                        >
                          <span className={date === today() ? "today-dot" : ""}>
                            {day.getDate()}
                          </span>
                          {visible
                            .filter(
                              (i: D) =>
                                (i.displayDate || i.dueDate)?.slice(0, 10) ===
                                date,
                            )
                            .map((i: D) => (
                              <button
                                className={
                                  "calendar-task " +
                                  (i.completed ? "completed" : "")
                                }
                                key={i.id}
                                draggable={false}
                                onPointerDown={(e) => beginReminderDrag(e, i)}
                                onDragStart={(e) => startDrag(e, i)}
                                onContextMenu={(e) =>
                                  showContext(e, reminderActions(i))
                                }
                                onClick={() => openDetail(i)}
                              >
                                <i
                                  style={{
                                    background: colorFor(
                                      lists.find((l: D) => l.id === i.listId),
                                    ),
                                  }}
                                />
                                {i.title}
                              </button>
                            ))}
                        </div>
                      );
                    })}
                  </div>
                  {visible.some((i: D) => !i.dueDate) && (
                    <div className="calendar-unscheduled">
                      <div className="section-heading">No date</div>
                      {visible
                        .filter((i: D) => !i.dueDate)
                        .map((i: D) => (
                          <button
                            key={i.id}
                            className="calendar-task"
                            draggable={false}
                            onPointerDown={(e) => beginReminderDrag(e, i)}
                            onDragStart={(e) => startDrag(e, i)}
                            onClick={() => openDetail(i)}
                          >
                            {i.title}
                          </button>
                        ))}
                    </div>
                  )}
                </div>
              ) : layout === "columns" ? (
                <div className="columns">
                  {Object.entries(groups).map(([name, items], index) => (
                    <section
                      className="column"
                      key={name}
                      data-drop-kind="section"
                      data-drop-section={data.sections.find((s: D) => s.title === name && s.listId === query.listId)?.objectUUID || ""}
                      onDragOver={(e) => {
                        e.preventDefault();
                        e.currentTarget.classList.add("drag-target");
                      }}
                      onDragLeave={(e) => {
                        if (!e.currentTarget.contains(e.relatedTarget as Node))
                          e.currentTarget.classList.remove("drag-target");
                      }}
                      onDrop={(e) => {
                        e.preventDefault();
                        e.currentTarget.classList.remove("drag-target");
                        const items = draggedItems(e);
                        if (
                          !items.length ||
                          !settings.advancedFeatures ||
                          !query.listId
                        )
                          return;
                        const section = data.sections.find(
                          (s: D) =>
                            s.title === name && s.listId === query.listId,
                        );
                        batchItems(items, "update_reminder", {
                          ...(section
                            ? { section_id: section.objectUUID }
                            : { no_section: true }),
                          private: true,
                        });
                      }}
                    >
                      <div
                        className="section-heading"
                        onContextMenu={(e) => {
                          const items = sectionActions(name);
                          if (items.length) showContext(e, items);
                        }}
                      >
                        <i style={{ background: COLORS[index % 6] }} />
                        <span>{name}</span>
                        <small>{items.length}</small>
                      </div>
                      {items.map(row)}
                      {!items.length && <div className="lane-empty">No reminders</div>}
                    </section>
                  ))}
                </div>
              ) : (
                <div
                  className="task-list"
                  role="listbox"
                  aria-label={heading}
                  aria-multiselectable="true"
                >
                  {Object.entries(groups).map(([name, items]) => (
                    <section key={name}>
                      {name !== "Reminders" && (
                        <button
                          className="section-heading section-disclosure"
                          aria-expanded={!collapsedSections.includes(name)}
                          aria-label={`${collapsedSections.includes(name) ? "Expand" : "Collapse"} section ${name}`}
                          onClick={() => {setCollapsedSections(old => old.includes(name) ? old.filter(v => v !== name) : [...old, name]); setSelection([]);}}
                          onContextMenu={(e) => {
                            const items = sectionActions(name);
                            if (items.length) showContext(e, items);
                          }}
                        >
                          {collapsedSections.includes(name) ? <ChevronRight size={13}/> : <ChevronDown size={13}/>}
                          {name}
                          <small>{items.length}</small>
                        </button>
                      )}
                      {!collapsedSections.includes(name) && items.map(row)}
                    </section>
                  ))}
                </div>
              )}
              {!loading && readFailed && !visible.length && (
                <div className="empty-state" role="status">
                  <AlertCircle size={28}/><h2>Couldn’t load reminders</h2>
                  <p>Your reminders haven’t been changed. Reopen Reminders from the app sidebar if this connection is closed.</p>
                  <button onClick={()=>refresh()}>Try again</button>
                </div>
              )}
              {!loading && !readFailed && !visible.length && (
                <div className="empty-state">
                  <div className="empty-orbit">
                    <Check size={27} />
                  </div>
                  <h2>
                    {surface === "selection"
                      ? "Bring a few reminders into focus"
                      : query.view === "today"
                        ? "A little room to breathe."
                        : "Nothing here yet."}
                  </h2>
                  <p>
                    {surface === "selection"
                      ? "Choose what this conversation should know."
                      : query.view === "deleted"
                        ? "Your recently deleted reminders will appear here."
                        : "Add a reminder whenever you’re ready."}
                  </p>
                  {surface === "selection" && (
                    <button className="primary" onClick={() => navigate({ view: "all" })}>Choose reminders</button>
                  )}
                </div>
              )}
              {loading && visible.length === 0 && (
                <div className="skeleton-list">
                  {[1, 2, 3, 4].map((n) => (
                    <div className="skeleton" key={n} />
                  ))}
                </div>
              )}
              {data.nextOffset != null && (
                <button
                  className="load-more"
                  onClick={() =>
                    refresh({ ...query, offset: data.nextOffset }, true)
                  }
                >
                  Load more · {data.total - visible.length} remaining
                </button>
              )}
              <footer className="collection-footer">
                <span>
                  <i className="connected-dot" />
                  Apple Reminders
                  {busy ? " · Working…" : loading ? " · Refreshing…" : ""}
                </span>
                <button onClick={() => setModal({ kind: "commands" })}>
                  Commands <kbd>⌘ K</kbd>
                </button>
              </footer>
            </div>
            {detail && (
              <Inspector
                key={detail.id}
                reload={async () => {
                  await refresh();
                  await openDetail(detail);
                }}
                openSubtask={openDetail}
                item={detail}
                lists={lists}
                sections={data.sections}
                settings={settings}
                close={() => setDetail(null)}
                busy={busy}
                drafts={drafts}
                save={(args: D, expectedRevision?: string) =>
                  run(() =>
                    change(
                      "update_reminder",
                      { reminder_id: detail.id, ...args },
                      {...detail, revision:expectedRevision || detail.revision},
                    ),
                  )
                }
                complete={() => complete(detail)}
                flag={() => flag(detail)}
                act={openAction}
                run={run}
                onDelete={() => setModal({ kind: "delete", item: detail })}
                onDiscuss={() =>
                  setModal({ kind: "conversation", items: [detail] })
                }
                onAttach={() =>
                  run(async () => {
                    await attach([detail]);
                    setAttached([detail.id]);
                    setToast("Attached to conversation");
                  })
                }
                deleted={query.view === "deleted"}
                restore={() =>
                  openAction("restore_reminder", {
                    reminder_id: detail.restoreId || detail.id,
                    private: true,
                  })
                }
              />
            )}
          </div>
        )}
        {toast && (
          <div className="toast" role="status">
            <CheckCircle2 size={16} />
            {toast}
          </div>
        )}
        {undo && (
          <div className="undo-toast">
            <span>Reminder updated</span>
            <button onClick={undoChange} disabled={busy}>
              <Undo2 size={14} />
              Undo
            </button>
            <button aria-label="Dismiss undo" onClick={() => setUndo(null)}>
              <X size={13} />
            </button>
          </div>
        )}
      </main>
      {quickOpen && quickDraft && <QuickAdd draft={quickDraft} update={setQuickDraft} lists={lists} busy={busy} error={error} close={() => setQuickOpen(false)} save={add}/>}
      {contextMenu && (
        <ContextMenu {...contextMenu} close={() => setContextMenu(null)} />
      )}
      {modal && (
        <Modal
          title={
            modal.kind === "smart" ? (modal.item ? "Edit Smart List" : "New Smart List") : modal.kind === "commands"
              ? "Commands"
              : modal.kind === "appearance"
                ? "List appearance"
                : modal.kind === "settings"
                  ? "RemCTL Settings"
                  : modal.kind === "action"
                    ? modal.tool.title
                    : modal.kind === "conversation"
                      ? "Work with ChatGPT"
                      : modal.kind === "delete"
                        ? "Delete reminder?"
                        : "Move reminders"
          }
          close={() => setModal(null)}
        >
          {error && <div className="error-banner" role="alert"><AlertCircle size={16} /><span>{error}</span></div>}
          {modal.kind === "smart" && <SmartListEditor item={modal.item} lists={lists} symbols={symbols} busy={busy} save={(args:D)=>run(async()=>{await change(modal.item?"manage_smart_list_edit":"manage_smart_list_create",args);setModal(null);setToast("Smart list saved");})}/>}
          {modal.kind === "commands" && (
            <CommandPalette actions={paletteActions} />
          )}
          {modal.kind === "appearance" && (
            <ListAppearance
              list={modal.list}
              symbols={symbols}
              disabled={busy}
              save={(args) =>
                run(async () => {
                  await change("update_list", args);
                  setModal(null);
                })
              }
            />
          )}
          {modal.kind === "settings" && (
            <Settings
              values={settings}
              lists={lists}
              save={(set) =>
                run(async () => {
                  const value = await call("update_settings", { set });
                  setSettings(value.values);
                  if (set.layout) {
                    defaultLayout.current = set.layout;
                    setLayout(layoutFor(query));
                  }
                  if ("showCompleted" in set) {
                    const next = {
                      ...query,
                      includeCompleted: set.showCompleted,
                    };
                    setQuery(next);
                    await refresh(next);
                  }
                  setToast("Settings saved");
                })
              }
            />
          )}
          {modal.kind === "action" && (
            <ActionForm
              tool={modal.tool}
              defaults={modal.defaults}
              advanced={settings.advancedFeatures}
              busy={busy}
              choices={{ lists, sections: data.sections, items: data.items, smartLists: data.smartLists }}
              onSubmit={(args) =>
                run(async () => {
                  const response = modal.tool.annotations?.readOnlyHint
                    ? await call(modal.tool.name, args)
                    : await change(modal.tool.name, args);
                  if (modal.tool.annotations?.readOnlyHint)
                    setModal({
                      kind: "action",
                      tool: modal.tool,
                      defaults: args,
                      response,
                    });
                  else {
                    setModal(null);
                    setToast("Done");
                  }
                })
              }
              response={modal.response}
            />
          )}
          {modal.kind === "conversation" && (modal.intent ? <div className="watch-review"><textarea aria-label="Monitoring request" rows={7} value={modal.intent} onChange={e=>setModal({...modal,intent:e.target.value})}/><button className="primary" disabled={busy} onClick={()=>run(async()=>{await discuss(modal.items,modal.intent,"new");setModal(null);})}>Start monitoring conversation</button></div> : (
            <div className="conversation-actions">
              {[
                "Help me prioritize these reminders.",
                "Break these reminders into practical next steps.",
                "Help me plan when to do these reminders.",
                "Summarize the progress on these reminders.",
              ].map((intent, index) => (
                <button
                  key={intent}
                  onClick={() =>
                    run(async () => {
                      await discuss(modal.items, intent);
                      setModal(null);
                    })
                  }
                >
                  <MessageSquare size={18} />
                  <span>
                    {
                      [
                        "Prioritize",
                        "Break into steps",
                        "Plan my time",
                        "Review progress",
                      ][index]
                    }
                  </span>
                  <ArrowUpRight size={16} />
                </button>
              ))}
              <button
                onClick={() =>
                  run(async () => {
                    await discuss(
                      modal.items,
                      "Help me work on these reminders.",
                      "new",
                    );
                    setModal(null);
                  })
                }
              >
                <Plus size={18} />
                <span>Start a new conversation</span>
                <ArrowUpRight size={16} />
              </button>
            </div>
          ))}
          {modal.kind === "delete" && (
            <div className="confirm">
              <p>“{modal.item.title}” will move to Recently Deleted.</p>
              <button
                className="danger-button"
                onClick={() =>
                  run(async () => {
                    await change("delete_reminder", {
                      reminder_id: modal.item.id,
                    });
                    setModal(null);
                    setDetail(null);
                  })
                }
              >
                Delete reminder
              </button>
            </div>
          )}
          {modal.kind === "bulkMove" && (
            <div className="move-picker">
              {lists
                .filter((l: D) => !l.isGroup)
                .map((l: D) => (
                  <button
                    key={l.id}
                    onClick={() => {
                      setModal(null);
                      bulk("update_reminder", { list_id: l.id });
                    }}
                  >
                    <List size={16} />
                    {l.title}
                    <ChevronRight size={14} />
                  </button>
                ))}
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement;
    const first =
      ref.current?.querySelector<HTMLElement>("input,textarea,select") ||
      ref.current?.querySelector<HTMLElement>("button");
    first?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Tab") {
        const controls = Array.from(
          ref.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled),input,select,textarea",
          ) || [],
        );
        if (e.shiftKey && document.activeElement === controls[0]) {
          e.preventDefault();
          controls.at(-1)?.focus();
        } else if (!e.shiftKey && document.activeElement === controls.at(-1)) {
          e.preventDefault();
          controls[0]?.focus();
        }
      }
    };
    ref.current?.addEventListener("keydown", key);
    return () => {
      ref.current?.removeEventListener("keydown", key);
      previousFocus?.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={ref}
      >
        <header>
          <h2>{title}</h2>
          <IconButton label="Close dialog" onClick={close}>
            <X size={17} />
          </IconButton>
        </header>
        {children}
      </div>
    </div>
  );
}
function Settings({
  values,
  save,
  lists,
}: {
  values: D;
  save: (set: D) => void;
  lists: D[];
}) {
  const [schema, setSchema] = useState<D>({});
  useEffect(() => {
    call("read_settings").then((s) => setSchema(s.schema.properties));
  }, []);
  return (
    <div className="settings-form">
      {Object.entries(schema).map(([key, s]: [string, any]) => (
        <label key={key} className="setting-row">
          <span>
            {key === "layout" ? "Default layout" : s.title}
            {key === "layout" && <small>Each list remembers the layout you pick for it.</small>}
            {key === "advancedFeatures" && (
              <small>Uses private ReminderKit features.</small>
            )}
          </span>
          {key === "defaultList" ? (
            <Select
              value={values[key]}
              onChange={(e) => save({ defaultList: e.target.value })}
              options={[{ value: "", label: "System default", text: "System default" }, ...listChoices(lists)]}
            />
          ) : s.type === "boolean" ? (
            <input
              type="checkbox"
              role="switch"
              checked={!!values[key]}
              onChange={(e) => save({ [key]: e.target.checked })}
            />
          ) : s.enum ? (
            <Select
              value={values[key]}
              onChange={(e) => save({ [key]: e.target.value })}
            >
              {s.enum.map((v: string) => (
                <option key={v} value={v}>
                  {titleCase(v)}
                </option>
              ))}
            </Select>
          ) : key === "refreshSeconds" ? (
            <Select
              value={values[key]}
              onChange={(e) => save({ [key]: Number(e.target.value) })}
            >
              {[...new Set([15, 30, 60, 120, 300, Number(values[key]) || 30])].sort((a, b) => a - b).map((n) => (
                <option key={n} value={n}>
                  {n < 60 ? `Every ${n} seconds` : n === 60 ? "Every minute" : `Every ${n / 60} minutes`}
                </option>
              ))}
            </Select>
          ) : (
            <input
              type={s.type === "integer" ? "number" : "text"}
              min={s.minimum}
              max={s.maximum}
              defaultValue={values[key]}
              onBlur={(e) => {
                const value =
                  s.type === "integer"
                    ? Number(e.target.value)
                    : e.target.value;
                if (value !== values[key]) save({ [key]: value });
              }}
            />
          )}
        </label>
      ))}
    </div>
  );
}
function ActionForm({
  tool,
  defaults = {},
  advanced,
  busy,
  choices,
  onSubmit,
  response,
}: {
  tool: D;
  defaults?: D;
  advanced: boolean;
  busy: boolean;
  choices: D;
  onSubmit: (args: D) => void;
  response?: D;
}) {
  const [values, setValues] = useState<D>(defaults);
  const props = tool.inputSchema.properties || {};
  const [templates, setTemplates] = useState<D[]>([]);
  useEffect(() => {
    if (props.template_id) call("manage_templates").then((response) => {
      setTemplates((response.items || []).map((item: D) => ({...item, title: item.name})));
    }).catch(() => {});
  }, [tool.name]);
  const [expanded, setExpanded] = useState(false);
  const requiresPrivate = !!props.private &&
    (tool.name.startsWith("manage_") || tool.name === "restore_reminder");
  const fields = Object.entries(props).filter(
    ([key]) => !["private", "force"].includes(key) &&
      !((key === "group" || key.endsWith("list")) && props[`${key}_id`]) &&
      !(key === "name" && ((tool.name.startsWith("manage_group_") && props.group_id) || props.template_id ||
        (props.smart_list_id && !tool.name.startsWith("manage_section_")))),
  );
  const common = new Set([
    ...(tool.inputSchema.required || []),
    ...Object.keys(defaults),
    "title",
    "name",
    "new_name",
    "new_name_option",
    "list_id",
    "notes",
    "due",
    "priority",
    "color",
    "emoji",
    "group_id", "template_id", "smart_list_id", "add_list_id", "tags", "flagged", "match", "date", "include_list_id",
  ]);
  const basic =
    fields.length <= 8 ? fields : fields.filter(([key]) => common.has(key));
  const extra =
    fields.length <= 8 ? [] : fields.filter(([key]) => !common.has(key));
  const optionsFor = (key: string) =>
    key === "section_id"
      ? choices.sections
          .filter((v: D) => !values.list_id || v.listId === values.list_id)
          .map((v: D) => ({ id: v.objectUUID, title: v.title }))
      : key === "template_id" ? templates
      : key === "smart_list_id" ? (choices.smartLists || []).filter((v: D) => v.kind === "custom").map((v: D) => ({...v, title: v.name}))
      : key.endsWith("list_id") || key === "group_id"
        ? choices.lists.filter((v: D) =>
            key === "group_id" ? v.isGroup : !v.isGroup,
          )
        : ["reminder_id", "before", "after", "parent_id"].includes(key)
          ? choices.items
          : null;
  const field = (key: string, s: D) => (
    <div key={key} className="form-field">
      <label htmlFor={`action-${key}`}>
        {titleCase(key.replace(/_id$/, ""))}
        {tool.inputSchema.required?.includes(key) ? " *" : ""}
      </label>
      {optionsFor(key) && s.type === "array" ? (
        <div className="form-choices" role="group" aria-label={titleCase(key.replace(/_id$/, ""))}>
          {optionsFor(key).map((v: D) => (
            <label key={v.id}>
              <input type="checkbox" checked={(values[key] || []).includes(v.id)} onChange={(e) => setValues({...values, [key]: e.target.checked ? [...(values[key] || []), v.id] : (values[key] || []).filter((id: number) => id !== v.id)})} />
              <span>{v.title}</span>
            </label>
          ))}
        </div>
      ) : optionsFor(key) ? (
        <Select
          id={`action-${key}`}
          value={values[key] ?? ""}
          required={tool.inputSchema.required?.includes(key)}
          onChange={(e) =>
            setValues({
              ...values,
              [key]:
                e.target.value === ""
                  ? ""
                  : s.type === "integer"
                    ? Number(e.target.value)
                    : e.target.value,
            })
          }
        >
          <option value="">Choose…</option>
          {optionsFor(key).map((v: D) => (
            <option key={v.id} value={v.id}>
              {v.title}
            </option>
          ))}
        </Select>
      ) : s.type === "boolean" ? (
        <input
          id={`action-${key}`}
          type="checkbox"
          checked={!!values[key]}
          onChange={(e) => setValues({ ...values, [key]: e.target.checked })}
        />
      ) : s.enum ? (
        <Select
          id={`action-${key}`}
          value={values[key] ?? ""}
          onChange={(e) => setValues({ ...values, [key]: e.target.value })}
        >
          <option value="">Default</option>
          {s.enum.map((v: string) => (
            <option key={v} value={v}>
              {titleCase(v)}
            </option>
          ))}
        </Select>
      ) : s.type === "array" || key === "notes" ? (
        <textarea
          id={`action-${key}`}
          placeholder={s.type === "array" ? "One per line" : ""}
          value={
            Array.isArray(values[key])
              ? values[key].join("\n")
              : values[key] || ""
          }
          onChange={(e) =>
            setValues({
              ...values,
              [key]:
                s.type === "array"
                  ? e.target.value
                      .split("\n")
                      .filter(Boolean)
                      .map((v) => (s.items?.type === "integer" ? Number(v) : v))
                  : e.target.value,
            })
          }
        />
      ) : (
        <input
          id={`action-${key}`}
          type={s.type === "integer" || s.type === "number" ? "number" : "text"}
          required={tool.inputSchema.required?.includes(key)}
          value={values[key] ?? ""}
          onChange={(e) =>
            setValues({
              ...values,
              [key]:
                s.type === "integer" || s.type === "number"
                  ? Number(e.target.value)
                  : e.target.value,
            })
          }
        />
      )}
    </div>
  );
  const submit = (e: React.SyntheticEvent) => {
        e.preventDefault();
        if (!e.currentTarget.closest("form")?.reportValidity()) return;
        const args = Object.fromEntries(
          Object.entries(values).filter(([, v]) => v !== ""),
        );
        const privateFields = [
          "tags",
          "set_tags",
          "remove_tags",
          "clear_tags",
          "section",
          "section_id",
          "new_section",
          "no_section",
          "smart_list",
          "smart_list_id",
          "set_url",
          "clear_urls",
          "messaging_contact",
          "clear_messaging_contact",
          "flagged",
          "assign",
          "unassign",
          "early_reminder",
          "clear_early_reminder",
          "urgent",
          "subtasks",
          "symbol",
          "emoji",
          "groceries",
          "grocery_locale",
          "group",
          "group_id",
          "location_address",
          "location_title",
          "latitude",
          "longitude",
          "proximity",
          "radius",
          "grocery",
          "standard",
        ];
        const listChange =
          tool.name === "update_list" &&
          Object.keys(args).some((k) => !["list", "list_id", "new_name", "private"].includes(k));
        if (
          props.private &&
          advanced &&
          (requiresPrivate || listChange ||
            Object.keys(args).some((k) => privateFields.includes(k)) ||
            (typeof args.color === "string" && args.color.startsWith("#")))
        )
          args.private = true;
        else delete args.private;
        if (props.force) args.force = true;
        onSubmit(args);
  };
  return (
    <form
      className="action-form"
      onSubmit={submit}
      onKeyDown={(e) => {
        if (e.key === "Enter" && e.target instanceof HTMLInputElement) submit(e);
      }}
    >
      {basic.map(([key, s]) => field(key, s as D))}
      {extra.length > 0 && (
        <>
          <button
            type="button"
            className="disclosure"
            onClick={() => setExpanded(!expanded)}
          >
            <SlidersHorizontal size={15} />
            More options
            <ChevronDown size={13} />
          </button>
          {expanded && extra.map(([key, s]) => field(key, s as D))}
        </>
      )}
      {requiresPrivate && !advanced && (
        <p className="form-note">
          Enable Advanced Reminders features in Settings to use this action.
        </p>
      )}
      <button
        className="primary"
        type="button"
        onClick={submit}
        disabled={busy || (requiresPrivate && !advanced)}
      >
        {busy ? "Working…" : tool.annotations?.readOnlyHint ? "Show" : "Apply"}
      </button>
      {response && (
        <div className="result-list">
          {(response.items || [response]).map((r: D, index: number) => (
            <div key={index}>
              <strong>{r.title || r.name || r.status || "Result"}</strong>
              {r.description && <p>{r.description}</p>}
              <details>
                <summary>Details</summary>
                <pre>{JSON.stringify(r, null, 2)}</pre>
              </details>
            </div>
          ))}
        </div>
      )}
    </form>
  );
}
function Inspector({
  item,
  lists,
  sections,
  settings,
  close,
  save,
  complete,
  flag,
  act,
  run,
  onDelete,
  onDiscuss,
  onAttach,
  deleted,
  restore,
  reload,
  openSubtask,
  drafts,
  busy,
}: any) {
  const [draft, updateDraft] = useState<D>(() => drafts.current.get(item.id)?.fields || {}),
    [more, setMore] = useState(false),
    [subtask, setSubtask] = useState("");
  const pendingDraft = useRef<D | null>(null);
  const draftRevision = useRef(drafts.current.get(item.id)?.revision || item.revision);
  const setDraft = (next: D | ((old: D) => D)) => {
    updateDraft(old => {
      const fields = typeof next === "function" ? next(old) : next;
      if (Object.keys(fields).length) drafts.current.set(item.id, {fields, revision: draftRevision.current});
      else drafts.current.delete(item.id);
      return fields;
    });
  };
  const value = (key: string, original: any = "") => draft[key] ?? original;
  const location =
    item.alarms?.find((a: D) => a.type === "location")?.location || {};
  const alarm = item.alarms?.find(
    (a: D) => a.type === "relative" || a.type === "absolute",
  );
  const alarmText =
    alarm?.type === "relative"
      ? `${Math.abs(alarm.relativeOffsetMinutes)}m`
      : alarm?.date?.slice(0, 16).replace("T", " ") || "";
  const set = (key: string, v: any) => {
    if (!Object.keys(draft).length) draftRevision.current = item.revision;
    setDraft(old => ({ ...old, [key]: v }));
  };
  useEffect(() => {
    if (pendingDraft.current) {setDraft(pendingDraft.current);pendingDraft.current=null;draftRevision.current=item.revision;}
    else if (!Object.keys(draft).length) draftRevision.current=item.revision;
  }, [item.revision]);
  const saveDraft = async () => {
    if (busy || !Object.keys(draft).length) return;
                    const args = { ...draft };
                    if (
                      ("radius" in args || "proximity" in args) &&
                      !args.location_address
                    ) {
                      if (location.address)
                        args.location_address = location.address;
                      else if (
                        location.latitude != null &&
                        location.longitude != null
                      ) {
                        args.latitude = location.latitude;
                        args.longitude = location.longitude;
                      }
                    }
                    if (
                      settings.advancedFeatures &&
                      Object.keys(args).some((k) =>
                        [
                          "tags",
                          "section_id",
                          "assign",
                          "early_reminder",
                          "urgent",
                          "subtasks",
                          "set_tags",
                          "location_address",
                          "proximity",
                          "radius",
                          "latitude",
                          "longitude",
                        ].includes(k),
                      )
                    )
                      args.private = true;
                    if (args.recurrence === "") args.recurrence = "clear";
                    if (args.early_reminder === "")
                      args.early_reminder = "clear";
                    if (args.alarm === "") args.alarm = "clear";
                    if (args.due === "") {
                      args.due = "clear";
                    }
                    if (args.assign === "") {
                      delete args.assign;
                      args.unassign = true;
                    }
                    if ("tags" in args) {
                      args.set_tags = args.tags;
                      delete args.tags;
                    }
                    if (args.section_id === "") {
                      delete args.section_id;
                      args.no_section = true;
                    }
                    const saved=await save(args,draftRevision.current);
                    if(saved && !["partial", "uncertain"].includes(saved.status)){setDraft({});draftRevision.current=item.revision;}
  };
  const addSubtask = () => {
    if (!subtask.trim()) return;
    run(async () => {
      await mutate("update_reminder", {
        reminder_id: item.id, subtasks: [subtask.trim()], private: true,
      });
      setSubtask("");
      await reload();
    });
  };
  return (
    <aside
      className="inspector"
      aria-label="Reminder details"
      onKeyDown={e => {if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {e.preventDefault(); e.stopPropagation(); saveDraft();}}}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("Files")) {
          e.preventDefault();
          e.currentTarget.classList.add("drag-target");
        }
      }}
      onDragLeave={(e) => e.currentTarget.classList.remove("drag-target")}
      onDrop={(e) => {
        e.preventDefault();
        e.currentTarget.classList.remove("drag-target");
        const files = Array.from(e.dataTransfer.files);
        if (files.length && settings.advancedFeatures)
          run(async () => {
            await attachImages(item, files);
            await reload();
          });
      }}
    >
      <div className="inspector-toolbar">
        <span>Details{Object.keys(draft).length > 0 && <small className="draft-label" title="Kept while this workspace stays open"> · Edited</small>}</span>
        <IconButton label="Close details" onClick={close}>
          <X size={17} />
        </IconButton>
      </div>
            {Object.keys(draft).length > 0 && (
              <div className="save-bar inspector-save">
                <button onClick={() => {setDraft({});draftRevision.current=item.revision;}}>Cancel</button>
                {draftRevision.current!==item.revision&&<span className="inline-error">Changed elsewhere. Your draft is preserved.</span>}
                <button
                  className="primary"
                  disabled={busy}
                  onClick={saveDraft}
                >
                  Save changes
                </button>
              </div>
            )}

      <div className="inspector-scroll">
        <div className="inspector-title">
          <button
            className={"check " + (item.completed ? "checked" : "")}
            onClick={complete}
            disabled={deleted}
            aria-label="Toggle completion"
          >
            {item.completed && <Check size={13} />}
          </button>
          <textarea
            aria-label="Reminder title"
            value={value("title", item.title)}
            onChange={(e) => set("title", e.target.value)}
            rows={2}
          />
        </div>
        {deleted ? (
          <button className="primary" onClick={restore}>
            Restore reminder
          </button>
        ) : (
          <>
            <textarea
              className="notes"
              aria-label="Notes"
              placeholder="Add notes"
              value={value("notes", item.notes)}
              onChange={(e) => set("notes", e.target.value)}
              rows={3}
            />
            <div className="inspector-rich-links"><RichLinks item={item} run={run}/></div>
            {!!item.attachments?.length && <details className="inspector-attachments" open><summary>Attachments <span>{item.attachments.length}</span></summary><AttachmentGallery item={item} run={run}/></details>}
            <div className="inspector-group">
              <label>
                <List size={16} />
                <span>List</span>
                <Select
                  value={value("list_id", item.listId || "")}
                  onChange={(e) => set("list_id", Number(e.target.value))}
                  options={[
                    ...(!lists.some((l: D) => l.id === item.listId) ? [{ value: String(item.listId), label: item.list || "Unavailable list", text: item.list || "" }] : []),
                    ...listChoices(lists),
                  ]}
                />
              </label>
              <DueEditor value={value("due", item.dueDate?.replace("T", " ").slice(0, item.allDay ? 10 : 16) || "")} change={v => set("due", v)} weekStartsOn={settings.weekStartsOn}/>
              <label>
                <Flag size={16} />
                <span>Flagged</span>
                <input
                  type="checkbox"
                  checked={!!item.flagged}
                  onChange={flag}
                />
              </label>
              <label>
                <span className="priority-icon">!</span>
                <span>Priority</span>
                <Select
                  value={value("priority", item.priority || "none")}
                  onChange={(e) => set("priority", e.target.value)}
                >
                  {["none", "low", "medium", "high"].map((p) => (
                    <option key={p} value={p}>{titleCase(p)}</option>
                  ))}
                </Select>
              </label>
              <label>
                <Repeat2 size={16} />
                <span>Repeat</span>
                <input
                  aria-label="Repeat rule"
                  placeholder={
                    item.recurrence ? "Repeats · edit rule" : "Never"
                  }
                  value={value("recurrence", recurrenceText(item.recurrence))}
                  onChange={(e) => set("recurrence", e.target.value)}
                />
                <Select
                  variant="icon"
                  aria-label="Repeat presets"
                  value={value("recurrence", recurrenceText(item.recurrence))}
                  onChange={(e) => set("recurrence", e.target.value)}
                >
                  <option value="">Never</option>
                  <option value="daily">Every day</option>
                  <option value="weekly mon,tue,wed,thu,fri">Every weekday</option>
                  <option value="weekly">Every week</option>
                  <option value="weekly x2">Every two weeks</option>
                  <option value="monthly">Every month</option>
                  <option value="monthly last-fri">Last Friday of the month</option>
                  <option value="yearly">Every year</option>
                </Select>
              </label>
              <label>
                <Link2 size={16} />
                <span>URL</span>
                <input
                  aria-label="URL"
                  value={value("url", "")}
                  placeholder="Add another link"
                  onChange={(e) => set("url", e.target.value)}
                />
              </label>
            </div>
            <div className="inspector-group">
              <div className="tag-field"><span><Tag size={15}/>Tags</span><TagEditor disabled={!settings.advancedFeatures} value={value("tags", item.tags?.join(", ") || "")} change={v=>set("tags",v)}/></div>
              <label>
                <Columns3 size={16} />
                <span>Section</span>
                <Select
                  disabled={!settings.advancedFeatures}
                  value={value(
                    "section_id",
                    sections.find(
                      (s: D) =>
                        s.title === item.section && s.listId === item.listId,
                    )?.objectUUID || "",
                  )}
                  onChange={(e) => set("section_id", e.target.value)}
                >
                  <option value="">None</option>
                  {sections
                    .filter(
                      (s: D) =>
                        s.listId === Number(value("list_id", item.listId)),
                    )
                    .map((s: D) => (
                      <option key={s.id} value={s.objectUUID}>
                        {s.title}
                      </option>
                    ))}
                </Select>
              </label>
              <label>
                <UserRound size={16} />
                <span>Assigned</span>
                <Select
                  disabled={!settings.advancedFeatures}
                  value={value(
                    "assign",
                    item.assignment?.assignee?.objectUUID || "",
                  )}
                  onChange={(e) => set("assign", e.target.value)}
                >
                  <option value="">Unassigned</option>
                  {item.sharees?.map((s: D) => (
                    <option key={s.id} value={s.objectUUID}>
                      {s.name}
                    </option>
                  ))}
                </Select>
              </label>
              <label>
                <Clock size={16} />
                <span>Early Reminder</span>
                <input
                  disabled={!settings.advancedFeatures}
                  aria-label="Early Reminder"
                  placeholder={item.earlyReminder ? "Custom alert" : "None"}
                  value={value(
                    "early_reminder",
                    earlyReminderText(item.earlyReminder),
                  )}
                  onChange={(e) => set("early_reminder", e.target.value)}
                />
              </label>
            </div>
            {lists.find((l:D)=>l.id===item.listId)?.isGroceries && <button className="text-action" disabled={busy||!settings.advancedFeatures} onClick={()=>run(async()=>{await mutate("update_reminder",{reminder_id:item.id,grocery:true,private:true},item.revision);await reload();})}>Categorize grocery item</button>}
            <button className="disclosure" onClick={() => setMore(!more)}>
              <MapPin size={15} />
              Location & more
              <ChevronDown size={13} />
            </button>
            {more && (
              <div className="inspector-group">
                {location.title && (
                  <div className="alert-summary">
                    {location.title} · {location.proximity} · {location.radius}{" "}
                    m
                  </div>
                )}
                <label>
                  <Clock size={16} />
                  <span>Alarm</span>
                  <input
                    aria-label="Alarm"
                    placeholder="None"
                    value={value("alarm", alarmText)}
                    onChange={(e) => set("alarm", e.target.value)}
                  />
                </label>
                <label>
                  <span>Location</span>
                  <input
                    aria-label="Location address"
                    placeholder="Street address"
                    disabled={!settings.advancedFeatures}
                    value={value("location_address", location.address || "")}
                    onChange={(e) => set("location_address", e.target.value)}
                  />
                </label>
                <label>
                  <span>Trigger</span>
                  <Select
                    disabled={!settings.advancedFeatures}
                    value={value("proximity", location.proximity || "arriving")}
                    onChange={(e) => set("proximity", e.target.value)}
                  >
                    <option value="arriving">Arriving</option>
                    <option value="leaving">Leaving</option>
                  </Select>
                </label>
                <label>
                  <span>Radius</span>
                  <input
                    type="number"
                    aria-label="Radius in meters"
                    placeholder="100 m"
                    disabled={!settings.advancedFeatures}
                    value={value("radius", location.radius || "")}
                    onChange={(e) => set("radius", Number(e.target.value))}
                  />
                </label>
                <label>
                  <span>Urgent</span>
                  <input
                    type="checkbox"
                    disabled={!settings.advancedFeatures}
                    checked={value("urgent", item.urgent || false)}
                    onChange={(e) => set("urgent", e.target.checked)}
                  />
                </label>
                <button
                  onClick={() =>
                    act("update_reminder", {
                      reminder_id: item.id,
                      private: !!settings.advancedFeatures,
                    })
                  }
                >
                  All reminder fields <ArrowUpRight size={12} />
                </button>
              </div>
            )}
            <div className="subtasks">
              <div className="section-heading">
                Subtasks<small>{item.subtasks?.length || 0}</small>
              </div>
              {item.subtasks?.map((s: D) => (
                <div key={s.id}>
                  <button
                    className={"check " + (s.completed ? "checked" : "")}
                    aria-label={
                      (s.completed ? "Mark incomplete: " : "Complete: ") +
                      s.title
                    }
                    onClick={() =>
                      run(async () => {
                        await mutate(
                          "set_completion",
                          { reminder_id: s.id, completed: !s.completed },
                          s.revision,
                        );
                        await reload();
                      })
                    }
                  >
                    {s.completed && <Check size={12} />}
                  </button>
                  <button
                    className="subtask-title"
                    onClick={() => openSubtask(s)}
                  >
                    {s.title}
                  </button>
                </div>
              ))}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  addSubtask();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { e.preventDefault(); addSubtask(); }
                }}
              >
                <Plus size={14} />
                <input
                  aria-label="New subtask"
                  placeholder="Add subtask"
                  value={subtask}
                  disabled={!settings.advancedFeatures}
                  onChange={(e) => setSubtask(e.target.value)}
                />
                {subtask.trim() && <button type="button" className="icon-button" aria-label="Add subtask" onClick={addSubtask}><CornerDownLeft size={15} /></button>}
              </form>
            </div>
            <label className="attachment-picker">
              <Paperclip size={14} />
              <span>Add images</span>
              <input
                aria-label="Add images"
                type="file"
                accept="image/png,image/jpeg,image/webp,image/heic"
                multiple
                disabled={!settings.advancedFeatures}
                onChange={(e) => {
                  const files = Array.from(e.target.files || []);
                  e.target.value = "";
                  if (files.length)
                    run(async () => {
                      await attachImages(item, files);
                      await reload();
                    });
                }}
              />
            </label>
            <button
              className="text-action"
              disabled={!settings.advancedFeatures}
              onClick={() =>
                run(async () => {
                  const chosen = await call("choose_reminder_details", {
                    listId: item.listId,
                    reminderId: item.id,
                  });
                  if (chosen.action === "accept") {
                    const content = { ...chosen.content };
                    const chosenImage = content.image;
                    const imagePayload = content.imagePayload;
                    delete content.image;
                    delete content.imagePayload;
                    setDraft({ ...draft, ...content });
                    let imageAdded = false;
                    if (imagePayload) {
                      await call("workspace_attach_image", {
                        operationId: crypto.randomUUID(), reminderId: item.id,
                        ...imagePayload,
                      });
                      imageAdded = true;
                    } else if (chosenImage) {
                      if (!chosenImage.startsWith("remctl://")) {
                        const resource = extensions.resources
                          ? await extensions.resources.read({ uri: chosenImage, representation: "blob" })
                          : await app.readServerResource({ uri: chosenImage,
                              _meta: { "openai/resource": { representation: "blob" } } });
                        const image = resource.contents[0];
                        if (!("blob" in image))
                          throw new Error("The image could not be read");
                        await call("workspace_attach_image", {
                          operationId: crypto.randomUUID(),
                          reminderId: item.id,
                          mimeType: image.mimeType,
                          data: image.blob,
                        });
                        imageAdded = true;
                      }
                    }
                    const nextDraft = { ...draft, ...content };
                    if (imageAdded) {
                      pendingDraft.current = nextDraft;
                      await reload();
                    }
                    setDraft(nextDraft);
                  }
                })
              }
            >
              <SlidersHorizontal size={14} />
              Choose details…
            </button>
            <div className="inspector-bottom">
              <button onClick={onAttach}>
                <Paperclip size={15} />
                Attach
              </button>
              <button onClick={onDiscuss}>
                <MessageSquare size={15} />
                Ask ChatGPT
              </button>
            </div>
            <div className="inspector-bottom">
              <button onClick={() => safeLink(item.deepLink)}>
                <ArrowUpRight size={14} />
                Open in Reminders
              </button>
              <IconButton
                label="Copy link"
                onClick={() =>
                  navigator.clipboard.writeText(
                    `codex://plugins/remctl@remctl-local/app/open_workspace?path=${encodeURIComponent("/reminder/" + item.objectUUID)}`,
                  )
                }
              >
                <Link2 size={14} />
              </IconButton>
              <IconButton label="Delete reminder" onClick={onDelete}>
                <Trash2 size={14} />
              </IconButton>
            </div>
          </>
        )}
      </div>

    </aside>
  );
}
function FileView({
  file,
  run,
  report,
}: {
  file: D;
  run: (fn: () => Promise<any>) => any;
  report: (err: any) => void;
}) {
  const [text, setText] = useState(""),
    [meta, setMeta] = useState<D>({}),
    [review, setReview] = useState<D | null>(null),
    [changed, setChanged] = useState(false),
    [editing, setEditing] = useState(false),
    [message, setMessage] = useState("");
  const changedRef = useRef(false);
  changedRef.current = changed;
  let document: D | null = null;
  try {
    document = JSON.parse(text);
  } catch {}
  const reminders: D[] = Array.isArray(document?.reminders)
    ? document.reminders
    : [];
  const load = async () => {
    if (!extensions.resources)
      throw new Error("This host does not support file resources.");
    const response = await extensions.resources.read({ uri: file.resourceUri, representation: "text" });
    const content = response.contents[0];
    if (!("text" in content))
      throw new Error("This file is not readable as text.");
    setText(content.text);
    setMeta(content.openaiMetadata || {});
    changedRef.current = false;
    setChanged(false);
  };
  useEffect(() => {
    ready.then(load).catch(report);
    const dispose = extensions.resources?.addUpdateHandler(({ params }) => {
      if (params.uri === file.resourceUri) {
        if (changedRef.current) {
          setMessage("The file changed. Reload before saving.");
          setMeta((m) => ({ ...m, stale: true }));
        } else load().catch(report);
      }
    });
    extensions.resources?.subscribe({ uri: file.resourceUri }).catch(() => {});
    return () => {
      dispose?.();
      extensions.resources
        ?.unsubscribe({ uri: file.resourceUri })
        .catch(() => {});
    };
  }, [file.resourceUri]);
  return (
    <div className="file-view">
      <div className="collection-heading">
        <div>
          <div className="eyebrow">RemCTL document</div>
          <h1>{file.name}</h1>
        </div>
        <div>
          <button onClick={() => run(load)}>Reload</button>
          <button
            className="primary"
            disabled={!meta.writable || !changed || meta.stale}
            onClick={() =>
              run(async () => {
                const result = await extensions.resources!.write(
                  file.resourceUri,
                  { text, ifMatch: meta.etag },
                );
                if (result?.outcome !== "saved")
                  throw new Error(
                    result?.outcome === "conflict"
                      ? "The file changed. Reload before saving."
                      : "The file is too large.",
                  );
                await load();
                setMessage("File saved");
              })
            }
          >
            Save file
          </button>
        </div>
      </div>
      {message && <p>{message}</p>}
      <div className="file-mode">
        <button aria-pressed={!editing} onClick={() => setEditing(false)}>
          Preview
        </button>
        <button aria-pressed={editing} onClick={() => setEditing(true)}>
          Edit document
        </button>
        <span>{reminders.length} reminders</span>
      </div>
      {!editing ? (
        <div className="file-preview">
          {reminders.map((item: D, index: number) => (
            <div key={index} className="task-row">
              <span className={"check " + (item.completed ? "checked" : "")}>
                {item.completed && <Check size={12} />}
              </span>
              <div className="task-copy">
                <strong>{item.title}</strong>
                <div className="task-meta">
                  <span>{item.list}</span>
                  <span>{dateText(item)}</span>
                  {item.tags?.map((tag: string) => (
                    <span key={tag}>#{tag}</span>
                  ))}
                </div>
                {item.notes && <p>{item.notes}</p>}
              </div>
              {item.flagged && <Flag size={14} />}
            </div>
          ))}
          {!document && (
            <p>
              The document contains invalid JSON. Open Edit document to repair
              it.
            </p>
          )}
        </div>
      ) : (
        <textarea
          className="file-editor"
          aria-label="RemCTL document"
          spellCheck={false}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setChanged(true);
            setReview(null);
          }}
        />
      )}
      <button
        className="primary"
        onClick={() =>
          run(async () =>
            setReview(
              await call("review_import", { document: JSON.parse(text) }),
            ),
          )
        }
      >
        Review import
      </button>
      {review && (
        <div className="import-review">
          <h2>{review.count} new reminders</h2>
          <p>
            Creates copies in Apple Reminders. Editing this file does not change
            your reminders.
          </p>
          {review.omittedFields?.length > 0 && (
            <p>Not imported: {review.omittedFields.join(", ")}.</p>
          )}
          {review.items.slice(0, 20).map((i: D, n: number) => (
            <div key={n}>
              {i.title}
              <small>{i.list}</small>
            </div>
          ))}
          <button
            className="primary"
            onClick={() =>
              run(async () => {
                const result = await call("import_remctl_file", {
                  reviewId: review.reviewId,
                  operationId: crypto.randomUUID(),
                });
                setMessage(`${result.created || 0} reminders imported`);
                setReview(null);
              })
            }
          >
            Import {review.count} reminders
          </button>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<Workspace />);
