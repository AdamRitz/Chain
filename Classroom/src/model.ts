import type { Row } from "./api";
export function MergeMessages(history: Row[], canonical: Row[]): Row[] {
  const records = new Map<string, Row>();
  for (const row of [...history, ...canonical]) records.set(row.id, row);
  return [...records.values()].sort(
    (a, b) =>
      a.height - b.height ||
      a.tx_index - b.tx_index ||
      a.log_index - b.log_index,
  );
}
export function CanSend(
  body: string,
  state: {
    unlocked: boolean;
    running: boolean;
    contractReady: boolean;
    roomOpen: boolean;
    busy: boolean;
  },
): boolean {
  return (
    state.unlocked &&
    state.running &&
    state.contractReady &&
    state.roomOpen &&
    !state.busy &&
    body.trim().length > 0 &&
    new TextEncoder().encode(body).length <= 512
  );
}
