import type { Metadata } from "next";

import "@/styles/common.css";
import "@/styles/landing.css";
import "@/styles/dashboard.css";
import "@/styles/theme.css";

export const metadata: Metadata = {
  title: "AccordAI | Accounting Intelligence",
  description:
    "AI-Driven Invoice Processing and Automated Record Standardisation. Company accounting intelligence platform.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: `(() => { try { const p = localStorage.getItem('accordai-theme') || 'system'; const d = p === 'dark' || (p === 'system' && matchMedia('(prefers-color-scheme: dark)').matches); document.documentElement.dataset.theme = d ? 'dark' : 'light'; document.documentElement.style.colorScheme = d ? 'dark' : 'light'; } catch (_) {} })();` }} />
      </head>
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
