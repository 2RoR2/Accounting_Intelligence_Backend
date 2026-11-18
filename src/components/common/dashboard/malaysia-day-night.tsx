"use client";

import { useEffect, useMemo, useState } from "react";

type Band = "sunrise" | "daylight" | "evening" | "night";
type LocationContext = { timeZone: string; city: string; country: string; currency: string; locale: string };

const LOCATION_BY_ZONE: Record<string, { city: string; country: string; currency: string }> = {
  "Asia/Kuala_Lumpur": { city: "Kuching", country: "Malaysia", currency: "MYR" },
  "Asia/Singapore": { city: "Singapore", country: "Singapore", currency: "SGD" },
  "Asia/Jakarta": { city: "Jakarta", country: "Indonesia", currency: "IDR" },
  "Asia/Bangkok": { city: "Bangkok", country: "Thailand", currency: "THB" },
  "Asia/Tokyo": { city: "Tokyo", country: "Japan", currency: "JPY" },
  "Asia/Seoul": { city: "Seoul", country: "South Korea", currency: "KRW" },
  "Europe/London": { city: "London", country: "United Kingdom", currency: "GBP" },
  "Europe/Paris": { city: "Paris", country: "France", currency: "EUR" },
  "America/New_York": { city: "New York", country: "United States", currency: "USD" },
  "America/Los_Angeles": { city: "Los Angeles", country: "United States", currency: "USD" },
};

function detectLocation(): LocationContext {
  const locale = Intl.DateTimeFormat().resolvedOptions().locale || "en-MY";
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kuala_Lumpur";
  const known = LOCATION_BY_ZONE[timeZone];
  if (known) return { ...known, timeZone, locale };
  const region = locale.split("-")[1]?.toUpperCase();
  const currencyByRegion: Record<string, string> = { AU: "AUD", CA: "CAD", CN: "CNY", DE: "EUR", ES: "EUR", HK: "HKD", IN: "INR", NZ: "NZD", PH: "PHP", US: "USD" };
  return { timeZone, locale, city: timeZone.split("/").pop()?.replaceAll("_", " ") || "Local time", country: region || "Local", currency: currencyByRegion[region || ""] || "MYR" };
}

function getMalaysiaState(date: Date, timeZone: string, locale: string) {
  const parts = new Intl.DateTimeFormat(locale, { timeZone, hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(date);
  const hour = Number(parts.find((part) => part.type === "hour")?.value ?? 0);
  const minute = Number(parts.find((part) => part.type === "minute")?.value ?? 0);
  const minutes = hour * 60 + minute;
  const band: Band = minutes < 360 ? "night" : minutes < 540 ? "sunrise" : minutes < 1020 ? "daylight" : minutes < 1140 ? "evening" : "night";
  return { hour, band };
}

export function MalaysiaDayNight() {
  const [now, setNow] = useState(() => new Date());
  const [location, setLocation] = useState<LocationContext>(() => ({ timeZone: "Asia/Kuala_Lumpur", city: "Kuching", country: "Malaysia", currency: "MYR", locale: "en-MY" }));
  useEffect(() => {
    setLocation(detectLocation());
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  const state = useMemo(() => getMalaysiaState(now, location.timeZone, location.locale), [now, location]);
  const time = new Intl.DateTimeFormat(location.locale, { timeZone: location.timeZone, hour: "2-digit", minute: "2-digit", hour12: false }).format(now);
  const date = new Intl.DateTimeFormat(location.locale, { timeZone: location.timeZone, day: "2-digit", month: "short", year: "numeric" }).format(now);
  const label = state.band === "sunrise" ? "Sunrise" : state.band === "daylight" ? "Daylight" : state.band === "evening" ? "Evening" : "Night";
  return (
    <div className={`malaysia-day-night malaysia-${state.band}`} aria-label={`${location.country} time ${time}, ${location.city}. ${label}. Currency ${location.currency}.`}>
      <div className="malaysia-landscape" aria-hidden="true"><span className="malaysia-sky-orb" /><i /><b /></div>
      <div className="malaysia-day-night-copy"><strong>{time}</strong><span>{location.city} · {location.country}</span><small>{label} · {date} · {location.currency}</small></div>
    </div>
  );
}
