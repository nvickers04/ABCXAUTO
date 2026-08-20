import type { ReactNode } from "react";

/** Passthrough — web-pro is operator UI only (no Better Auth in this package). */
export function AuthProvider({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
