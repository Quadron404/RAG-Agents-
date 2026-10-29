import { useCallback, useState } from "react";
import { Check, Mail, Send, X } from "lucide-react";
import { useUi } from "../store";
import {
  denyConnectorApproval,
  executeConnectorApproval,
  type ConnectorApproval,
} from "../lib/routines";
import { IconButton, Pill } from "./ui";

function connectorIcon(connector: string) {
  if (connector === "gmail") return <Mail size={16} />;
  return <Send size={16} />;
}

function statusTone(status: string) {
  switch (status) {
    case "allowed":
      return "green";
    case "denied":
      return "red";
    case "expired":
      return "amber";
    default:
      return "default";
  }
}

export function ApprovalCard({
  approval,
  onResolved,
}: {
  approval: ConnectorApproval;
  onResolved?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const toast = useUi((s) => s.toast);

  const argumentsText = (() => {
    const args = approval.arguments || {};
    if (approval.action === "send_email" && typeof args.content === "string") {
      return args.content;
    }
    return JSON.stringify(args, null, 2);
  })();

  const handleAllow = useCallback(async () => {
    setBusy(true);
    const result = await executeConnectorApproval(approval.approval_id);
    setBusy(false);
    if (!result.ok) {
      toast(result.error || "Could not approve the action.", "err");
      return;
    }
    toast("Action approved and executed.", "ok");
    onResolved?.();
  }, [approval.approval_id, toast, onResolved]);

  const handleDeny = useCallback(async () => {
    setBusy(true);
    const result = await denyConnectorApproval(approval.approval_id);
    setBusy(false);
    if (!result.ok) {
      toast(result.error || "Could not deny the action.", "err");
      return;
    }
    toast("Action denied.", "ok");
    onResolved?.();
  }, [approval.approval_id, toast, onResolved]);

  const expired = approval.status !== "pending";
  const expires = new Date(approval.expires_at * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className="approval-card">
      <div className="approval-card__head">
        <div className="approval-card__connector">
          {connectorIcon(approval.connector)}
          <span className="approval-card__connector-name">{approval.connector}</span>
        </div>
        <Pill tone={statusTone(approval.status)}>{approval.status}</Pill>
      </div>

      <div className="approval-card__body">
        <div className="approval-card__field">
          <span className="approval-card__label">Requested action</span>
          <span className="approval-card__value">{approval.action}</span>
        </div>
        <div className="approval-card__field">
          <span className="approval-card__label">Account</span>
          <span className="approval-card__value">
            Your connected {approval.connector} account
          </span>
        </div>
        {approval.action === "send_email" ? (
          <>
            <div className="approval-card__field">
              <span className="approval-card__label">Recipient</span>
              <span className="approval-card__value">Your verified email (fixed — cannot be changed)</span>
            </div>
            <div className="approval-card__field approval-card__field--content">
              <span className="approval-card__label">Message</span>
              <div className="approval-card__message">{argumentsText}</div>
            </div>
          </>
        ) : (
          <div className="approval-card__field">
            <span className="approval-card__label">Details</span>
            <div className="approval-card__message">{argumentsText}</div>
          </div>
        )}
        <div className="approval-card__field">
          <span className="approval-card__label">Expires</span>
          <span className="approval-card__value">{expires}</span>
        </div>
        <div className="approval-card__note">
          No action is taken until you choose Allow or Deny. This approval is
          single-use and cannot be reused.
        </div>
      </div>

      <div className="approval-card__actions">
        {!expired ? (
          <>
            <IconButton
              icon={Send}
              onClick={handleAllow}
              disabled={busy}
              title="Allow"
              ariaLabel="Allow this connector action"
            />
            <IconButton
              icon={X}
              onClick={handleDeny}
              disabled={busy}
              title="Deny"
              ariaLabel="Deny this connector action"
            />
          </>
        ) : null}
      </div>
    </div>
  );
}

export function ApprovalsList({
  approvals,
  onResolved,
}: {
  approvals: ConnectorApproval[];
  onResolved: () => void;
}) {
  if (!approvals.length) return null;
  return (
    <div className="approvals-list">
      <div className="approvals-list__title">
        <Check size={15} />
        Pending connector approvals
      </div>
      <div className="approvals-list__items">
        {approvals.map((a) => (
          <ApprovalCard key={a.approval_id} approval={a} onResolved={onResolved} />
        ))}
      </div>
    </div>
  );
}
