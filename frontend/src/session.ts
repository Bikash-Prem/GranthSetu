import { useEffect, useState } from "react";
import { api, onAuthChange, type User } from "./api";

/** The signed-in user, re-checked with the server whenever any request returns 401 (expired or revoked session). */
let current: User | null = null;
let loaded = false;
let registrationOpen = true;
const subs = new Set<() => void>();

export async function refreshSession(): Promise<User | null> {
  try {
    const r = await api.get<{ user: User | null; registration_open: boolean }>("/auth/me");
    current = r.user;
    registrationOpen = r.registration_open;
  } catch {
    current = null;
  }
  loaded = true;
  subs.forEach((f) => f());
  return current;
}

onAuthChange(() => {
  if (current) refreshSession();
});

export function useSession() {
  const [, tick] = useState(0);
  useEffect(() => {
    const f = () => tick((n) => n + 1);
    subs.add(f);
    if (!loaded) refreshSession();
    return () => {
      subs.delete(f);
    };
  }, []);
  const can = (perm: string) => !!current?.permissions.includes(perm);
  return { user: current, loaded, registrationOpen, can, refresh: refreshSession };
}
