import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ApprovalCard, ApprovalsList } from "../src/components/ApprovalCard";
import type { ConnectorApproval } from "../src/lib/routines";

const baseApproval: ConnectorApproval = {
  approval_id: "appr-1",
  connector: "gmail",
  action: "send_email",
  arguments: { content: "Hello world", sender_email: "bot@example.com" },
  status: "pending",
  created_at: Date.now(),
  expires_at: Date.now() + 30 * 60 * 1000,
  routine_id: "",
  routine_run_id: "",
};

describe("ApprovalCard", () => {
  it("renders pending approval with Allow/Deny buttons", () => {
    render(<ApprovalCard approval={baseApproval} onResolved={() => {}} />);
    expect(screen.getByText("gmail")).not.toBeNull();
    expect(screen.getByText("send_email")).not.toBeNull();
    expect(screen.getByTitle("Allow")).not.toBeNull();
    expect(screen.getByTitle("Deny")).not.toBeNull();
  });

  it("shows decided status without buttons", () => {
    const decided = { ...baseApproval, status: "allowed" };
    const { container } = render(<ApprovalCard approval={decided} onResolved={() => {}} />);
    expect(container.querySelector('button[title="Allow"]')).toBeNull();
    expect(container.querySelector('button[title="Deny"]')).toBeNull();
  });

  it("renders ApprovalsList with multiple approvals", () => {
    render(
      <ApprovalsList
        approvals={[baseApproval, { ...baseApproval, approval_id: "appr-2", status: "denied" }]}
        onResolved={() => {}}
      />,
    );
    const gmailElements = screen.getAllByText("gmail", { exact: true });
    expect(gmailElements.length).toBeGreaterThanOrEqual(2);
  });
});
