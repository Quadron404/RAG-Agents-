/** Resolving the authenticated principal from the edge session cookie. */
import type { Env } from "./env";
import { sessionTokenFromRequest } from "./http";
import { getValidSession, getUserById, type SessionRow, type UserRow } from "./store";

export interface Principal {
  user: UserRow;
  session: SessionRow;
}

export async function resolvePrincipal(
  env: Env,
  request: Request,
): Promise<Principal | null> {
  const token = sessionTokenFromRequest(request);
  if (!token) return null;
  const session = await getValidSession(env, token);
  if (!session) return null;
  const user = await getUserById(env, session.user_id);
  if (!user) return null;
  return { user, session };
}
