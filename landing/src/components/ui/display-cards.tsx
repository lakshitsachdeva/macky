"use client";

import type { CSSProperties, ReactNode } from "react";
import { Sparkles } from "lucide-react";

export interface DisplayCardProps {
  icon?: ReactNode;
  title?: string;
  description?: string;
  date?: string;
  eyebrow?: string;
  accent?: string;
}

interface DisplayCardsProps {
  cards?: DisplayCardProps[];
}

function DisplayCard({
  icon = <Sparkles size={18} strokeWidth={1.8} />,
  title = "Featured",
  description = "Discover amazing content",
  date = "Just now",
  eyebrow = "Memory fragment",
  accent = "rgba(200,169,110,0.65)",
}: DisplayCardProps) {
  return (
    <article className="memory-card" style={{ "--memory-accent": accent } as CSSProperties}>
      <div className="memory-card__glow" />
      <div className="memory-card__top">
        <span className="memory-card__icon">{icon}</span>
        <span className="memory-card__eyebrow">{eyebrow}</span>
      </div>
      <div className="memory-card__body">
        <h3 className="memory-card__title">{title}</h3>
        <p className="memory-card__description">{description}</p>
      </div>
      <p className="memory-card__date">{date}</p>
    </article>
  );
}

export default function DisplayCards({ cards }: DisplayCardsProps) {
  const defaultCards: DisplayCardProps[] = [
    {
      title: "Mail trail",
      description: "Sarah's deck from two Tuesdays ago",
      date: "mail attachment",
      eyebrow: "Sender memory",
    },
    {
      title: "Downloaded PDF",
      description: "That invoice you opened twice",
      date: "downloads",
      eyebrow: "Open count",
    },
    {
      title: "Screenshot memory",
      description: "The apartment listing with the blue kitchen",
      date: "screenshots",
      eyebrow: "Visual clue",
    },
  ];

  const displayCards = cards || defaultCards;

  return (
    <div className="memory-card-stack">
      {displayCards.map((cardProps, index) => (
        <div
          key={`${cardProps.title ?? "card"}-${index}`}
          className={`memory-card-stack__slot memory-card-stack__slot--${index + 1}`}
        >
          <DisplayCard {...cardProps} />
        </div>
      ))}
    </div>
  );
}
