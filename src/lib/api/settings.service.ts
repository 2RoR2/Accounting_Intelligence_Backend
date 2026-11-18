import type { User } from "@/types";
import { api, get } from "./client";
import { requireRole } from "../permissions";
export async function getSettings(user: User) {
  void user;
  return get<{
    timezone: string;
    notifications: string;
    sessionTimeoutMinutes: number;
    defaultDocumentView: "all" | "exceptions" | "review";
    emailAssignmentNotifications: boolean;
    processingStatusNotifications: boolean;
    uploadMaxSizeMb: number;
    allowCameraUploads: boolean;
  }>("/api/settings");
}
export async function saveSettings(
  user: User,
  settings: {
    timezone: string;
    notifications: string;
    sessionTimeoutMinutes: number;
    defaultDocumentView: "all" | "exceptions" | "review";
    emailAssignmentNotifications: boolean;
    processingStatusNotifications: boolean;
    uploadMaxSizeMb: number;
    allowCameraUploads: boolean;
  },
) {
  requireRole(user, ["SUPER_ADMIN", "LOCAL_ADMIN", "ACCOUNTANT"]);
  await api("/api/settings", { method: "PUT", body: JSON.stringify(settings) });
}
