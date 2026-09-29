import type { Metadata } from "next";
import { Archivo, JetBrains_Mono } from "next/font/google";
import "./globals.css";

// One variable family; width (wdth 100 for text, 116-125 for display) carries the hierarchy.
const archivo = Archivo({ subsets: ["latin"], axes: ["wdth"], variable: "--font-archivo", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = {
  title: "AegisOps",
  description: "Autonomous reliability and security control plane",
};

// Applies the saved theme before paint to avoid a flash of the wrong theme.
const themeScript = `try{var t=localStorage.getItem("aegis-theme");if(t)document.documentElement.dataset.theme=t;}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" className={`${archivo.variable} ${mono.variable}`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="canvas min-h-screen font-sans antialiased">{children}</body>
    </html>
  );
}
