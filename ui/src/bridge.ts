import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";
import { OpenAIExtensions } from "@openai/mcp-extensions/app";
import contextIcon from "../../assets/remctl-context-icon.png";
export const app = new App(
  { name: "RemCTL", version: "2.5.0" },
  {},
  { autoResize: true },
);
export const extensions = new OpenAIExtensions(app);
export type RecordData = Record<string, any>;
export async function attachImages(item: RecordData, files: File[]) {
  if (!files.length) return;
  for (const file of files) {
    if (
      !["image/png", "image/jpeg", "image/webp", "image/heic"].includes(
        file.type,
      ) ||
      file.size > 8 * 1024 * 1024
    )
      throw new Error(
        "Choose PNG, JPEG, WebP or HEIC images smaller than 8 MB.",
      );
  }
  for (const file of files) {
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = () => reject(new Error("The image could not be read."));
      reader.readAsDataURL(file);
    });
    await mutate("_attach_image", {
      reminderId: item.id,
      mimeType: file.type,
      data,
      private: true,
    });
  }
}
let latest: RecordData | undefined;
const listeners = new Set<(value: RecordData) => void>();
export function subscribe(fn: (value: RecordData) => void) {
  listeners.add(fn);
  if (latest) fn(latest);
  return () => {
    listeners.delete(fn);
  };
}
function emit(value: RecordData) {
  latest = value;
  listeners.forEach((fn) => fn(value));
}
app.ontoolresult = (result) => emit({ type: "result", result });
app.ontoolinput = (params) => {
  if (params.arguments?.file)
    emit({ type: "file", file: params.arguments.file });
};
// The host's floating composer can report its space as a bottom safe-area inset.
function applyInsets(context: any) {
  const bottom = context?.safeAreaInsets?.bottom;
  if (typeof bottom === "number") document.documentElement.style.setProperty("--host-safe-bottom", `${bottom}px`);
}
app.onhostcontextchanged = (context) => {
  applyInsets(context);
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables)
    applyHostStyleVariables(context.styles.variables);
  listeners.forEach((fn) => fn({ type: "context", context }));
};
export const ready = app
  .connect()
  .then(() => {
    const context = app.getHostContext();
    applyInsets(context);
    if (context?.theme) applyDocumentTheme(context.theme);
    if (context?.styles?.variables)
      applyHostStyleVariables(context.styles.variables);
    listeners.forEach((fn) => fn({ type: "context", context }));
  })
  .catch((error) => emit({ type: "error", message: String(error) }));
export async function call(name: string, args: RecordData = {}) {
  await ready;
  const response = await app.callServerTool(
    { name, arguments: args },
    { timeout: name === "choose_reminder_details" ? 900000 : 180000 },
  );
  const data = (response.structuredContent as RecordData) || {};
  if (response.isError)
    throw Object.assign(
      new Error(
        data.message ||
          data.error?.message ||
          (response.content.find((c) => c.type === "text") as any)?.text ||
          "Action failed",
      ),
      { data },
    );
  return data;
}
export async function mutate(
  tool: string,
  args: RecordData,
  revision?: string,
) {
  const key = "remctl-operation:" + JSON.stringify([tool, args, revision]);
  let operationId: string;
  try {
    operationId = sessionStorage.getItem(key) || crypto.randomUUID();
    sessionStorage.setItem(key, operationId);
  } catch {
    operationId = crypto.randomUUID();
  }
  try {
    const value = await call("workspace_mutate", {
      tool,
      arguments: args,
      operationId,
      ...(revision ? { expectedRevision: revision } : {}),
    });
    try {
      sessionStorage.removeItem(key);
    } catch {}
    return value;
  } catch (error: any) {
    const code = error.data?.error?.code || error.data?.code;
    if (
      ["invalid_argument", "conflict"].includes(code) ||
      error.data?.status === "conflict"
    ) {
      try {
        sessionStorage.removeItem(key);
      } catch {}
    }
    throw error;
  }
}
export async function attach(items: RecordData[]) {
  if (!extensions.modelContext)
    throw new Error("This host does not support selected context.");
  return extensions.modelContext.update({
    content: items.map((i) => ({
      type: "text" as const,
      text: `${i.title} (${i.list || "Reminders"}, RemCTL ID ${i.id}${i.resourceUri ? ", " + i.resourceUri : ""})`,
      _meta: {
        "openai/title": i.title,
        "openai/thumbnail": { src: contextIcon, mimeType: "image/png" },
        "remctl/id": i.id,
      },
    })),
    structuredContent: {
      items: items.map(({ id, title, resourceUri, list }) => ({
        id,
        title,
        resourceUri,
        list,
      })),
    },
  });
}
export async function discuss(
  items: RecordData[],
  intent: string,
  target: "active" | "new" = "active",
) {
  if (!extensions.message)
    throw new Error("Conversation actions are unavailable in this host.");
  await extensions.message.send({
    role: "user",
    content: [
      {
        type: "text",
        text: `${intent}\n\n${items.map((i) => `${i.title} — ${i.list || ""} (RemCTL ID ${i.id}${i.resourceUri ? ", " + i.resourceUri : ""})`).join("\n")}`,
      },
    ],
    _meta: { "openai/message": { target, send: true } },
  });
}
// Schemes that read local files, run script, or reach file servers. Keep in
// sync with BLOCKED_LINK_SCHEMES in remctl_runtime.py.
const BLOCKED_LINK_SCHEMES = new Set([
  "about", "afp", "blob", "data", "file", "ftp", "ftps", "javascript", "nfs",
  "sftp", "smb", "ssh", "telnet", "vbscript", "vnc", "ws", "wss",
]);
export function linkScheme(url: string) {
  return /^([a-z][a-z0-9+.-]*):./i.exec(url || "")?.[1].toLowerCase() || "";
}
// An app link (obsidian://, things:, x-apple-reminderkit:) opens in its app.
export function isAppLink(url: string) {
  const scheme = linkScheme(url);
  return !!scheme && scheme !== "http" && scheme !== "https" && !BLOCKED_LINK_SCHEMES.has(scheme);
}
export function safeLink(url: string) {
  return /^https?:\/\/./i.test(url) || isAppLink(url)
    ? app.openLink({ url })
    : Promise.reject(new Error("Unsupported link"));
}
