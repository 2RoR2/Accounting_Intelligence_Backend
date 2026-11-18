export interface ExtractionWarning { code: string; message: string; reviewed?: boolean; }
export type EvidenceStatus = "source_backed" | "ambiguous_candidate" | "missing" | "conflicting" | "human_corrected" | "model_source_verified" | "model_suggested" | "unsupported";
export interface OcrEngineScore { min: number; max: number; scope: "text_recognition_only"; }
export interface SourceEvidence { page?: number; source_ref?: string; text?: string; bbox?: { l: number; t: number; r: number; b: number; coord_origin?: string } | null; page_size?: { width: number; height: number } | null; coordinate_units?: string; text_span?: [number, number]; bbox_scope?: string; source_note?: string; method?: string; }
export type AiAssistOutcome = "PARSER_PRESERVED" | "SOURCE_VERIFIED_CANDIDATE" | "UNVERIFIED_MODEL_SUGGESTION" | "PARSER_MODEL_CONFLICT" | "NO_RECOVERY" | "MODEL_FAILURE";
export interface AiAssistResultField { field: string; stored_key: string; outcome: AiAssistOutcome; candidate?: string | null; parser_value?: string | null; current_value?: string | null; source_check?: string; evidence?: SourceEvidence[]; request_reason?: string; explanation: string; }
export interface AiAssistResult { status: string; attempt_id?: string; model?: string; model_digest?: string | null; page?: number; seconds?: number; message?: string; fields: AiAssistResultField[]; requestedBy?: string; requestedAt?: string; }
export interface AiAssistState { available: boolean; supportedFields: Array<{ field: string; storedKey: string }>; latest: AiAssistResult | null; decisions: Array<{ field: string; decision: "accepted" | "rejected"; attempt_id?: string | null; by: string; at: string }>; }
export interface CandidateEvidence extends SourceEvidence { printed_label?: string; printed_value?: string; extraction_method?: string; candidate_basis?: string; }
export interface FieldCandidate { source?: string; value?: string | null; raw_text?: string | null; source_check?: string; model?: string | null; evidence?: CandidateEvidence[]; }
export interface ExtractedField {
  label: string; value: string | null; original: string | null; confidence: number | null;
  rawValue?: string | null; normalizedValue?: string | null; currentValue?: string | null;
  warnings?: ExtractionWarning[]; confirmationRequired?: boolean; reviewRequired?: boolean;
  state?: string; resolution?: string; evidence?: SourceEvidence[]; candidates?: unknown[];
  method?: string; modelName?: string | null; modelVersion?: string | null;
  evidenceStatus?: EvidenceStatus; ocrEngineScore?: OcrEngineScore | null;
  correctedBy?: string | null; correctedAt?: string | null;
}
export interface LineCorrection { line_item_id: string; field_key: string; old_value: string | null; new_value: string | null; corrected_by: string; corrected_at: string; }
export interface LineItem { methods?: Record<string, string | null>; fieldStates?: Record<string, string>; guidance?: Record<string, Pick<ExtractedField, "warnings" | "confirmationRequired" | "reviewRequired">>; id?: string; description: string | null; quantity: string | number | null; unitPrice: string | number | null; amount: string | number | null; evidence?: Record<string, SourceEvidence[]>; candidates?: Record<string, unknown[]>; }
export interface SourceClassification {
  value?: string; status?: string; method?: string; reason?: string; classifier_version?: string;
  candidates?: Array<{ value?: string; matched_title?: string; evidence?: { text?: string; source_ref?: string; prov?: Array<{ page_no?: number; bbox?: Record<string, unknown>; charspan?: number[] }> } }>;
  unsupported_headings?: Array<{ matched_title?: string; evidence?: { text?: string; source_ref?: string; prov?: Array<{ page_no?: number; bbox?: Record<string, unknown>; charspan?: number[] }> } }>;
}
export interface CurrencyClue { kind: string; name: string; currency: string; text: string; why: string; page?: number | null; }
export interface CurrencyProposal { rank: number; currency: string; status: "proposed_not_verified"; support: "printed_on_document" | "supplier_records" | "document_context"; clues: CurrencyClue[]; }
export interface CurrencyDecision { value: string; previous?: string | null; selectedRank?: number | null; selectedBasis?: Array<{ kind: string; text: string }>; proposalsShown?: string[]; sourceVerified: false; by?: string; at?: string; }
export interface CurrencyReview { version: string; needed: boolean; printedSymbol?: { symbol: string; text?: string | null; page?: number | null } | null; proposals: CurrencyProposal[]; alternatives: string[]; conflicts: Array<{ currency: string; name: string; text: string }>; disagreement: boolean; sourceVerified: false; statement: string; decision?: CurrencyDecision; }
export interface Extraction { currencyReview?: CurrencyReview | null; lineCorrections?: LineCorrection[]; warnings?: ExtractionWarning[]; documentId: string; contentRevision?: number; raw?: Record<string, unknown>; classification?: SourceClassification; fields: Record<string, ExtractedField>; lineItems: LineItem[]; }
