import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

def process_document_mock(document_uuid: str) -> Dict[str, Any]:
    """
    Mocked CPU task receiving ONLY the immutable document_uuid.
    
    LIMITATION (Milestone 3A): PostgreSQL ownership verification is NOT implemented
    merely because the worker receives document_uuid. In the future, the worker must 
    query the DB to verify tenant ownership and document existence.
    
    LIMITATION (Milestone 3A): The durable outbox, dispatcher, leases, and recovery 
    sweeper remain future implementation work. Dual-write consistency is NOT solved 
    in this milestone.
    
    This mocked job returns minimal metadata. RQ stores job return values in Redis, 
    so we DO NOT return invoice numbers, financial fields, source documents, or extraction 
    JSON from queue jobs.
    """
    logger.info(f"Mock processing document UUID: {document_uuid}")
    
    # Return minimal JSON metadata to avoid storing sensitive data in Redis
    return {
        "status": "mock_completed",
        "document_uuid": document_uuid
    }

