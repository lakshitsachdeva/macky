"use client";

import { FileText, ImageIcon, Mail } from "lucide-react";
import DisplayCards from "@/components/ui/display-cards";

const defaultCards = [
  {
    icon: <Mail size={18} strokeWidth={1.8} />,
    title: "Mail trail",
    description: "Sarah's deck from two Tuesdays ago",
    date: "mail attachment",
    eyebrow: "Sender memory",
    accent: "rgba(212, 181, 120, 0.72)",
  },
  {
    icon: <FileText size={18} strokeWidth={1.8} />,
    title: "Downloaded PDF",
    description: "That invoice you opened twice",
    date: "downloads",
    eyebrow: "Open count",
    accent: "rgba(126, 184, 212, 0.7)",
  },
  {
    icon: <ImageIcon size={18} strokeWidth={1.8} />,
    title: "Screenshot memory",
    description: "The apartment listing with the blue kitchen",
    date: "screenshots",
    eyebrow: "Visual clue",
    accent: "rgba(152, 198, 170, 0.68)",
  },
];

export function DisplayCardsDemo() {
  return <DisplayCards cards={defaultCards} />;
}
