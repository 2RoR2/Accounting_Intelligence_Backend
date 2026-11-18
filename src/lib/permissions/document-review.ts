import type { Document, User } from "@/types";
import type { DocumentReviews } from "@/lib/api/workflow.service";
import { canDocument } from "./index";
/** UI affordances only. The backend enforces every action independently. */
export function canEditDocument(user: User, doc: Document, hasRecord = false): boolean {
  return ["LOCAL_ADMIN", "ACCOUNTANT"].includes(user.role) && !!user.tenantId && canDocument(user, doc)
    && !hasRecord && !["COMPLETED", "STANDARDISED"].includes(doc.status);
}
export function canDecideReview(user: User, doc: Document, reviews: DocumentReviews | null): boolean {
  // GET reviews has no submission revision; conservatively use the latest actor.
  // The server additionally checks current revision and distinct submitter.
  const latest = reviews?.submissions[0];
  return user.role === "LOCAL_ADMIN" && canEditDocument(user, doc) && doc.status === "IN_REVIEW"
    && !!latest && latest.decision === "SUBMITTED" && latest.submittedBy !== user.id;
}
export function hasCurrentApproval(reviews: DocumentReviews | null, revision: number | undefined): boolean {
  return !!revision && !!reviews?.decisions.some(decision => decision.decision === "APPROVED" && decision.approvedRevision === revision);
}
