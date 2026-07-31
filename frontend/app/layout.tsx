import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Sentellent — Indian Equity Analyst",
  description:
    "An agentic RAG research assistant for NSE and BSE equities. Grounded, cited answers in INR.",
  icons: { icon: "/icon.png", apple: "/icon.png" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
