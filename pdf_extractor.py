"""
Simplified PDF extraction interface with better error handling.
Provides a cleaner abstraction over the pdf_form.extract module.
"""

from typing import Tuple, Optional, Dict, Any
from dataclasses import dataclass
from pdf_form import extract_acroform, NoAcroFormFieldsError, NotAcroFormError
from pdf_form.schema import FormSchema
from pypdf import PdfReader
from io import BytesIO
from config import MAX_FILE_SIZE, ERROR_MESSAGES


@dataclass
class PDFValidationResult:
    """Result of PDF validation and parsing."""
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    schema: Optional[FormSchema] = None
    warnings: list = None
    
    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class PDFExtractor:
    """Simplified interface for PDF form extraction with comprehensive error handling."""
    
    @staticmethod
    def validate_pdf_file(file_bytes: bytes, filename: str) -> PDFValidationResult:
        """
        Validate a PDF file for form extraction.
        
        Args:
            file_bytes: Raw PDF file bytes
            filename: Original filename
            
        Returns:
            PDFValidationResult with validation status and any errors/warnings
        """
        # Size validation
        if len(file_bytes) > MAX_FILE_SIZE:
            return PDFValidationResult(
                success=False,
                error_code='file_too_large',
                error_message=ERROR_MESSAGES['file_too_large']
            )
        
        # PDF format validation
        if not file_bytes.startswith(b'%PDF'):
            return PDFValidationResult(
                success=False,
                error_code='not_pdf',
                error_message=ERROR_MESSAGES['not_pdf']
            )
        
        # Encryption check
        try:
            reader = PdfReader(BytesIO(file_bytes))
            if getattr(reader, 'is_encrypted', False):
                return PDFValidationResult(
                    success=False,
                    error_code='encrypted_pdf',
                    error_message=ERROR_MESSAGES['encrypted_pdf']
                )
        except Exception:
            # If we can't read the PDF at all, it's probably corrupted
            return PDFValidationResult(
                success=False,
                error_code='parse_failed',
                error_message='PDF file appears to be corrupted'
            )
        
        return PDFValidationResult(success=True)
    
    @staticmethod
    def extract_form_schema(file_bytes: bytes, filename: str) -> PDFValidationResult:
        """
        Extract form schema from a validated PDF file.
        
        Args:
            file_bytes: Raw PDF file bytes
            filename: Original filename
            
        Returns:
            PDFValidationResult with schema if successful, or error details
        """
        # First validate the file
        validation = PDFExtractor.validate_pdf_file(file_bytes, filename)
        if not validation.success:
            return validation
        
        # Extract form schema
        try:
            schema = extract_acroform(file_bytes, filename)
            
            # Check for warnings based on metadata
            warnings = []
            if schema.metadata.get('total_fields_raw', 0) > len(schema.fields):
                warnings.append('fields_truncated')
            if schema.metadata.get('truncated_to_first_page'):
                warnings.append('first_page_only')
            
            return PDFValidationResult(
                success=True,
                schema=schema,
                warnings=warnings
            )
            
        except NotAcroFormError:
            return PDFValidationResult(
                success=False,
                error_code='not_acroform',
                error_message=ERROR_MESSAGES['not_acroform']
            )
            
        except NoAcroFormFieldsError:
            return PDFValidationResult(
                success=False,
                error_code='no_fields',
                error_message=ERROR_MESSAGES['no_fields']
            )
            
        except Exception as e:
            return PDFValidationResult(
                success=False,
                error_code='parse_failed',
                error_message=f"PDF parsing failed: {str(e)}"
            )
    
    @staticmethod
    def process_uploaded_pdf(file_bytes: bytes, filename: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Process an uploaded PDF file and return a standardized response.
        
        Args:
            file_bytes: Raw PDF file bytes
            filename: Original filename
            
        Returns:
            Tuple of (success: bool, response_data: dict)
        """
        result = PDFExtractor.extract_form_schema(file_bytes, filename)
        
        if not result.success:
            return False, {
                'ok': False,
                'error': result.error_code,
                'message': result.error_message
            }
        
        # Build successful response
        response = {
            'ok': True,
            'schema': result.schema.to_public_dict()
        }
        
        if result.warnings:
            response['warnings'] = result.warnings
        
        return True, response
    
    @staticmethod
    def get_field_summary(schema: FormSchema) -> Dict[str, Any]:
        """
        Get a summary of form fields for logging/debugging.
        
        Args:
            schema: Form schema
            
        Returns:
            Dictionary with field summary information
        """
        field_names = schema.ordered_field_names()
        return {
            'total_fields': len(field_names),
            'field_names': field_names[:10],  # First 10 for preview
            'form_id': schema.form_id,
            'metadata': {
                'original_filename': schema.metadata.get('original_filename'),
                'total_fields_raw': schema.metadata.get('total_fields_raw', 0),
                'truncated_to_first_page': schema.metadata.get('truncated_to_first_page', False)
            }
        }


class PDFExtractionError(Exception):
    """Custom exception for PDF extraction errors."""
    
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        self.message = message
        super().__init__(message)


def extract_pdf_form_safe(file_bytes: bytes, filename: str) -> FormSchema:
    """
    Safe wrapper for PDF form extraction that raises PDFExtractionError on failure.
    
    Args:
        file_bytes: Raw PDF file bytes
        filename: Original filename
        
    Returns:
        FormSchema if successful
        
    Raises:
        PDFExtractionError: If extraction fails for any reason
    """
    result = PDFExtractor.extract_form_schema(file_bytes, filename)
    
    if not result.success:
        raise PDFExtractionError(result.error_code, result.error_message)
    
    return result.schema