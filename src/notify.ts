/**
 * Channel the worker uses to talk back to the human. Implemented by the
 * Telegram bot, but kept as an interface so the worker doesn't depend on
 * grammY directly (and so it can be swapped/tested).
 */
export interface Notifier {
  /** Fire-and-forget progress line to a chat. */
  send(chatId: number, text: string): Promise<void>;
  /**
   * Ask the human to approve a sensitive action. Resolves true (allow) or
   * false (deny). Should render inline Yes/No buttons.
   */
  requestApproval(chatId: number, req: ApprovalRequest): Promise<boolean>;
}

export interface ApprovalRequest {
  taskId: number;
  /** Human-readable summary of what the agent wants to do. */
  summary: string;
}
