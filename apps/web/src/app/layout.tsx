import type { ReactNode } from "react";
import "./globals.css";

export const metadata = {
  title: "Nexora",
  description: "One Platform. Every Intelligence.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
