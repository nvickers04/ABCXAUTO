import { createRootRoute, HeadContent, Outlet, Scripts } from "@tanstack/react-router";
import { AuthProvider } from "@/lib/auth/provider";
import appCss from "../styles.css?url";

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      {
        title: "ABCXAUTO Pro — Agentic Portfolio Cockpit",
      },
      {
        name: "description",
        content:
          "X-inspired agentic portfolio manager for IBKR paper trading. Control + Unbiased. Risk > execution > monitoring > thin UI.",
      },
    ],
    links: [{ rel: "stylesheet", href: appCss }],
  }),
  component: () => (
    <html lang="en" suppressHydrationWarning>
      <head>
        <HeadContent />
      </head>
      <body className="bg-bg text-fg antialiased">
        <AuthProvider>
          <Outlet />
        </AuthProvider>
        <Scripts />
      </body>
    </html>
  ),
});
