/** 调试日志追踪（对齐 app/utils/debug_trace.ts） */
export interface DebugTraceEntry {
  id: number;
  ts: number;
  scope: string;
  message: string;
  detail?: string;
}

const MAX_ENTRIES = 200;
const entries: DebugTraceEntry[] = [];
let nextId = 1;

function toDetailText(detail: string | null): string {
  if (detail === null || detail.length === 0) {
    return '';
  }
  return detail;
}

/** 追加调试日志 */
export function addDebugTrace(scope: string, message: string, detail?: string): void {
  const entry: DebugTraceEntry = {
    id: nextId,
    ts: Date.now(),
    scope: scope,
    message: message,
    detail: detail !== undefined ? toDetailText(detail) : undefined,
  };
  nextId += 1;
  entries.push(entry);
  if (entries.length > MAX_ENTRIES) {
    entries.splice(0, entries.length - MAX_ENTRIES);
  }
}

/** 清空调试日志 */
export function clearDebugTrace(): void {
  entries.length = 0;
}

/** 获取全部调试日志（最近 200 条） */
export function getDebugTrace(): DebugTraceEntry[] {
  return entries.slice();
}

/** 格式化时间戳（对齐 formatDebugTs） */
export function formatDebugTs(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, '0')}`;
}
