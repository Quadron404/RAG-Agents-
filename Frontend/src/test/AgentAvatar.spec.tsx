import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { AgentAvatar } from "../components/AgentAvatar";

function countDots(container: HTMLElement): number {
  return container.querySelectorAll(".agent-avatar__dot").length;
}

function getAvatar(container: HTMLElement): HTMLElement {
  const el = container.querySelector(".agent-avatar");
  if (!el) throw new Error("no agent-avatar element");
  return el as HTMLElement;
}

describe("AgentAvatar", () => {
  it("renders the canonical 3x3 dot grid", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" size={40} />);
    expect(countDots(container)).toBe(9);
  });

  it("renders a black rounded-square surface with a glass edge", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" size={40} />);
    const avatar = getAvatar(container);
    expect(avatar.style.borderRadius).toBe("9px");
  });

  it("derives a deterministic accent from agentId (same agent, same colour)", () => {
    const { container: c1 } = render(<AgentAvatar agentId="agent-1" name="Test" size={40} />);
    const { container: c2 } = render(<AgentAvatar agentId="agent-1" name="Test" size={40} />);
    const { container: c3 } = render(<AgentAvatar agentId="agent-2" name="Test" size={40} />);
    const dots1 = Array.from(c1.querySelectorAll(".agent-avatar__dot"));
    const dots2 = Array.from(c2.querySelectorAll(".agent-avatar__dot"));
    const dots3 = Array.from(c3.querySelectorAll(".agent-avatar__dot"));
    const bg1 = (dots1[0] as HTMLElement).style.backgroundColor;
    const bg2 = (dots2[0] as HTMLElement).style.backgroundColor;
    const bg3 = (dots3[0] as HTMLElement).style.backgroundColor;
    expect(bg1).toBe(bg2);
    expect(bg1).toBeTruthy();
    expect(bg3).toBeTruthy();
  });

  it("applies accessible name from name and role", () => {
    const { container } = render(
      <AgentAvatar agentId="agent-1" name="Chief" role="commander" status="idle" size={40} />,
    );
    const avatar = getAvatar(container);
    const label = avatar.getAttribute("aria-label") ?? "";
    expect(label).toContain("Chief");
    expect(label).toContain("commander");
  });

  it("hides from assistive tech when decorative", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" size={40} decorative={true} />);
    const avatar = getAvatar(container);
    expect(avatar.getAttribute("aria-hidden")).toBe("true");
  });

  it("supports idle animation without crash", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" status="idle" animation="idle" size={40} />);
    expect(countDots(container)).toBe(9);
  });

  it("supports thinking animation without crash", () => {
    const { container } = render(
      <AgentAvatar agentId="agent-1" name="Test" status="thinking" animation="thinking" size={40} />,
    );
    expect(countDots(container)).toBe(9);
  });

  it("supports working animation without crash", () => {
    const { container } = render(
      <AgentAvatar agentId="agent-1" name="Test" status="working" animation="working" size={40} />,
    );
    expect(countDots(container)).toBe(9);
  });

  it("renders completed status", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" status="completed" size={40} />);
    expect(getAvatar(container)).not.toBeNull();
  });

  it("renders failed status", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" status="failed" size={40} />);
    expect(getAvatar(container)).not.toBeNull();
  });

  it("renders cancelled status", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" status="cancelled" size={40} />);
    expect(getAvatar(container)).not.toBeNull();
  });

  it("renders offline status", () => {
    const { container } = render(<AgentAvatar agentId="agent-1" name="Test" status="offline" size={40} />);
    expect(getAvatar(container)).not.toBeNull();
  });

  it("supports different sizes without breaking dot geometry", () => {
    const { container: c1 } = render(<AgentAvatar agentId="agent-1" name="Test" size={20} />);
    const { container: c2 } = render(<AgentAvatar agentId="agent-1" name="Test" size={80} />);
    expect(countDots(c1)).toBe(9);
    expect(countDots(c2)).toBe(9);
  });

  it("disables animation under reduced motion", () => {
    const { container } = render(
      <AgentAvatar agentId="agent-1" name="Test" status="working" animation="working" size={40} reducedMotion={true} />,
    );
    const dots = container.querySelectorAll(".agent-avatar__dot");
    dots.forEach((dot) => {
      const name = (dot as HTMLElement).style.animationName;
      expect(name).not.toContain("agentAvatar");
    });
  });
});
