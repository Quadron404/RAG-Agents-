import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import {
  createAgentIdentity,
  createAgentAssignment,
  createAgentExecution,
  finishAgentExecution,
  getAgentAssignment,
  getAgentExecution,
  getAgentIdentity,
  getAgentIdentityBySlug,
  listAgentAssignments,
  listAgentExecutions,
  listAgentIdentities,
  updateAgentAssignmentStatus,
  updateAgentIdentity,
} from "../src/store";

let seq = 0;
function ownerSub(): string {
  seq += 1;
  return `sub-${seq}`;
}

function ownerId(): string {
  return `user-${seq}`;
}

describe("agent identity store", () => {
  it("creates and retrieves an agent identity", async () => {
    const sub = ownerSub();
    const id = await createAgentIdentity(env, {
      agentId: "agent-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "Researcher",
      slug: "researcher",
      role: "specialist",
      description: "Web and sources",
      specialization: "search, extraction",
      capabilityProfile: { tools: ["web_search", "fetch_url"] },
    });
    expect(id.agent_id).toBe("agent-1");
    expect(id.name).toBe("Researcher");
    expect(id.identity_version).toBe(1);

    const fetched = await getAgentIdentity(env, "agent-1");
    expect(fetched).not.toBeNull();
    expect(fetched?.owner_sub).toBe(sub);
    expect(fetched?.capability_profile).toContain("web_search");
  });

  it("looks up identity by slug scoped to owner", async () => {
    const sub = ownerSub();
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "Agent One",
      slug: "alpha",
    });
    const found = await getAgentIdentityBySlug(env, sub, "alpha");
    expect(found?.agent_id).toBe("a-1");
    // A different owner must not find the same slug.
    const other = await getAgentIdentityBySlug(env, `other-${sub}`, "alpha");
    expect(other).toBeNull();
  });

  it("lists identities scoped to owner", async () => {
    const sub = ownerSub();
    const other = `other-${sub}`;
    await createAgentIdentity(env, { agentId: "a-1", ownerSub: sub, ownerId: ownerId(), name: "A", slug: "a" });
    await createAgentIdentity(env, { agentId: "a-2", ownerSub: sub, ownerId: ownerId(), name: "B", slug: "b" });
    await createAgentIdentity(env, { agentId: "o-1", ownerSub: other, ownerId: ownerId(), name: "Other", slug: "o" });

    const mine = await listAgentIdentities(env, sub);
    expect(mine.length).toBe(2);
    expect(mine.map((i) => i.agent_id).sort()).toEqual(["a-1", "a-2"]);

    const theirs = await listAgentIdentities(env, other);
    expect(theirs.length).toBe(1);
    expect(theirs[0].agent_id).toBe("o-1");
  });

  it("updating identity increments version and does not overwrite historical fields silently", async () => {
    const sub = ownerSub();
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "Original",
      slug: "orig",
      description: "first description",
    });
    const first = await getAgentIdentity(env, "a-1");
    expect(first?.identity_version).toBe(1);

    const updated = await updateAgentIdentity(env, "a-1", {
      name: "Renamed",
      description: "second description",
    });
    expect(updated).not.toBeNull();
    expect(updated?.identity_version).toBe(2);
    expect(updated?.name).toBe("Renamed");
    expect(updated?.description).toBe("second description");

    // Version changed.
    const after = await getAgentIdentity(env, "a-1");
    expect(after?.identity_version).toBe(2);
  });

  it("slug is unique per owner", async () => {
    const sub = ownerSub();
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "One",
      slug: "shared",
    });
    // Same owner, same slug: upsert updates the existing row, no duplicate.
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "One updated",
      slug: "shared",
    });
    const all = await listAgentIdentities(env, sub);
    expect(all.filter((i) => i.slug === "shared").length).toBe(1);
  });
});

describe("agent assignment store", () => {
  it("creates and lists assignments scoped to agent and owner", async () => {
    const sub = ownerSub();
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "A",
      slug: "a",
    });
    await createAgentAssignment(env, {
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      taskPrompt: "Do the task",
      expectedDeliverable: "A result",
      outputSchema: { type: "object" },
    });
    await createAgentAssignment(env, {
      assignmentId: "asg-2",
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      taskPrompt: "Do another task",
    });

    const byAgent = await listAgentAssignments(env, sub, "a-1");
    expect(byAgent.length).toBe(2);
    expect(byAgent.map((a) => a.assignment_id).sort()).toEqual(["asg-1", "asg-2"]);

    const fetched = await getAgentAssignment(env, "asg-1");
    expect(fetched?.task_prompt).toBe("Do the task");
    expect(fetched?.expected_deliverable).toBe("A result");
    expect(fetched?.output_schema).toContain("object");
    expect(fetched?.status).toBe("pending");
  });

  it("new assignment does not overwrite identity fields", async () => {
    const sub = ownerSub();
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "Identity Name",
      slug: "a",
    });
    // There is no direct setter for identity_version in the store, but creation
    // sets version 1. An assignment is a separate table and never touches identity.
    await createAgentAssignment(env, {
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      taskPrompt: "Task",
    });
    const identity = await getAgentIdentity(env, "a-1");
    expect(identity?.identity_version).toBe(1);
    expect(identity?.name).toBe("Identity Name");
  });

  it("updating assignment status works", async () => {
    const sub = ownerSub();
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "A",
      slug: "a",
    });
    await createAgentAssignment(env, {
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      taskPrompt: "Task",
    });
    const ok = await updateAgentAssignmentStatus(env, "asg-1", "completed");
    expect(ok).toBe(true);
    const row = await getAgentAssignment(env, "asg-1");
    expect(row?.status).toBe("completed");
  });

  it("assignments are owner-isolated", async () => {
    const sub = ownerSub();
    const other = `other-${sub}`;
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "A",
      slug: "a",
    });
    await createAgentAssignment(env, {
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      taskPrompt: "Task",
    });
    const mine = await listAgentAssignments(env, sub);
    expect(mine.length).toBe(1);
    const theirs = await listAgentAssignments(env, other);
    expect(theirs.length).toBe(0);
  });
});

describe("agent execution store", () => {
  it("creates and finishes executions, persisting identity version", async () => {
    const sub = ownerSub();
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "A",
      slug: "a",
    });
    await createAgentAssignment(env, {
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      taskPrompt: "Task",
    });
    const exec = await createAgentExecution(env, {
      executionId: "ex-1",
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      identityVersion: 1,
    });
    expect(exec.status).toBe("pending");
    expect(exec.identity_version).toBe(1);

    const finished = await finishAgentExecution(env, "ex-1", {
      status: "completed",
      result: "the answer",
    });
    expect(finished).toBe(true);

    const row = await getAgentExecution(env, "ex-1");
    expect(row?.status).toBe("completed");
    expect(row?.result).toBe("the answer");
    expect(row?.completed_at).not.toBeNull();
  });

  it("executions are owner-isolated", async () => {
    const sub = ownerSub();
    const other = `other-${sub}`;
    await createAgentIdentity(env, {
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      name: "A",
      slug: "a",
    });
    await createAgentAssignment(env, {
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      ownerId: ownerId(),
      taskPrompt: "Task",
    });
    await createAgentExecution(env, {
      executionId: "ex-1",
      assignmentId: "asg-1",
      agentId: "a-1",
      ownerSub: sub,
      identityVersion: 1,
    });
    const mine = await listAgentExecutions(env, sub);
    expect(mine.length).toBe(1);
    const theirs = await listAgentExecutions(env, other);
    expect(theirs.length).toBe(0);
  });
});
