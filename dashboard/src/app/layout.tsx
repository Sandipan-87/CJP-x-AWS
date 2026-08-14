import type { Metadata } from "next";
import { Geist, JetBrains_Mono } from "next/font/google";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

// Strict monospace for numbers, timestamps, and raw SQL only -- everything else (headings,
// labels, body text) stays in Geist Sans, see globals.css's --font-sans/--font-mono wiring.
const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Engram Dashboard",
  description: "Read-only SSE surface over the Engram memory cluster (design/02-low-level-design.md §11).",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  // Dark mode only, no toggle -- this is an internal ops console, not a public-facing app
  // with user preference to respect. `.dark` (see globals.css) drives every theme token,
  // so setting it here once is the whole mechanism; no next-themes, no boot script, no
  // flash-of-light-then-dark risk since there is no light render to flash to.
  return (
    <html
      lang="en"
      className={`dark ${geistSans.variable} ${jetbrainsMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <TooltipProvider>{children}</TooltipProvider>
      </body>
    </html>
  );
}
