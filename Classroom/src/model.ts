import type { Row } from "./api";
export function NodeLesson(state: Row) {
  const demo = !state.network || state.network.mode === "demo";
  const count = demo ? 3 : 1;
  const nodes: Row[] = state.nodes || [];
  return {
    title: demo ? "启动三个演示节点" : "启动本机课堂节点",
    text: demo
      ? "一键创建三个本地节点，观察它们怎样保存相同账本。"
      : "本机运行一个节点，与老师和同学的电脑共同组网。",
    done:
      !!state.running &&
      state.network?.ports?.length === count &&
      nodes.length === count &&
      nodes.every((node) => node.online),
  };
}
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
export function SelectedMessage(
  selected: Row | null,
  canonical: Row[],
): Row | undefined {
  if (!selected?.body) return undefined;
  return canonical.find((row) => row.tx_hash === selected.tx_hash);
}
export function RestartMatches(checkpoint: Row | null, state: Row): boolean {
  return !!(
    checkpoint?.head &&
    state.running &&
    state.contract_ready &&
    state.network?.id === checkpoint.id &&
    state.stats?.head === checkpoint.head
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
