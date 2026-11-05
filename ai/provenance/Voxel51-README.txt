---
annotations_creators: []
language: en
size_categories:
- 1K<n<10K
task_ids: []
pretty_name: high_quality_invoice_images_ocr
tags:
- fiftyone
- image
- ocr
- visual-document-retrieval
dataset_summary: >




  This is a [FiftyOne](https://github.com/voxel51/fiftyone) dataset with 8106
  samples.


  ## Installation


  If you haven't already, install FiftyOne:


  ```bash

  pip install -U fiftyone

  ```


  ## Usage


  ```python

  import fiftyone as fo

  from fiftyone.utils.huggingface import load_from_hub


  # Load the dataset

  # Note: other available arguments include 'max_samples', etc

  dataset = load_from_hub("Voxel51/high-quality-invoice-images-for-ocr")


  # Launch the App

  session = fo.launch_app(dataset)

  ```
license: odbl
---

# Dataset Card for high_quality_invoice_images_ocr
![image/png](high-quality-invoice-images-for-ocr.gif)



This is a [FiftyOne](https://github.com/voxel51/fiftyone) dataset containing **8,181 high-quality synthetic invoice images** for OCR and document understanding tasks. The dataset includes 1,489 fully annotated samples with structured JSON metadata and raw OCR text, plus 6,692 unannotated images for semi-supervised learning or annotation projects.


## Installation

If you haven't already, install FiftyOne:

```bash
pip install -U fiftyone
```

## Usage

```python
import fiftyone as fo
from fiftyone.utils.huggingface import load_from_hub

# Load the dataset
# Note: other available arguments include 'max_samples', etc
dataset = load_from_hub("Voxel51/high-quality-invoice-images-for-ocr")

# Launch the App
session = fo.launch_app(dataset)
```


## Dataset Details

### Dataset Description

This dataset contains synthetic invoice images with tabular layouts, designed for training and evaluating OCR models, document understanding systems, and invoice processing pipelines. Each invoice includes seller and client information, itemized line items, tax calculations, and payment details.

- **Original Source:** [Kaggle - High Quality Invoice Images for OCR](https://www.kaggle.com/datasets/osamahosamabdellatif/high-quality-invoice-images-for-ocr)
- **Curated by:** Osama Hosam Abdellatif (original), Harpreet Sahota (FiftyOne port)
- **Language(s):** English
- **License:** Original license from Kaggle source

### Dataset Sources

- **Original Repository:** [Kaggle Dataset](https://www.kaggle.com/datasets/osamahosamabdellatif/high-quality-invoice-images-for-ocr)

## Uses

### Direct Use

This dataset is suitable for:
- **OCR Model Training**: Train text extraction models on structured document layouts
- **Document Understanding**: Develop models that extract structured information from invoices
- **Key-Value Extraction**: Train systems to identify and extract specific fields (invoice number, dates, totals, etc.)
- **Table Detection**: Practice detecting and parsing tabular data in documents
- **Layout Analysis**: Understand document structure and spatial relationships
- **Semi-Supervised Learning**: Use annotated samples to label the remaining 6,692 images
- **Model Evaluation**: Benchmark OCR and document AI models against ground truth annotations

### Out-of-Scope Use

- **Real Invoice Processing**: These are synthetic invoices and may not capture all variations in real-world invoice formats
- **Financial Analysis**: The data is synthetic and does not represent real transactions
- **Privacy-Sensitive Applications**: While synthetic, the dataset contains realistic addresses and names that should not be assumed to be real

## Dataset Structure

### FiftyOne Schema

Each sample in the dataset has the following structure:

```python
{
    'id': str,                      # Unique sample ID
    'media_type': 'image',          # Always 'image'
    'filepath': str,                # Absolute path to invoice image
    'tags': [],                     # Empty list (no tags)
    'metadata': {                   # Image metadata
        'size_bytes': int,          # File size in bytes
        'mime_type': 'image/jpeg',  # MIME type
        'width': int,               # Image width in pixels (typically ~1650px)
        'height': int,              # Image height in pixels (typically ~2300px)
        'num_channels': 3,          # RGB channels
    },
    'json_annotation': str | None,  # Structured JSON string (see schema below)
    'ocr_text': str | None,         # Raw OCR text output
}
```

### JSON Annotation Schema

For the 1,489 annotated samples, the `json_annotation` field contains a JSON string with the following structure:

```json
{
    "invoice": {
        "client_name": "string",
        "client_address": "string (may contain newlines)",
        "seller_name": "string",
        "seller_address": "string (may contain newlines)",
        "invoice_number": "string",
        "invoice_date": "string (MM/DD/YYYY format)",
        "due_date": "string (may be empty)"
    },
    "items": [
        {
            "description": "string (may contain newlines)",
            "quantity": "string (numeric format: X.XX)",
            "total_price": "string (numeric format: XX.XX)"
        }
    ],
    "subtotal": {
        "tax": "string (numeric)",
        "discount": "string (may be empty)",
        "total": "string (numeric)"
    },
    "payment_instructions": {
        "due_date": "string (may be empty)",
        "bank_name": "string (may be empty)",
        "account_number": "string (may be empty)",
        "payment_method": "string (may be empty)"
    }
}
```

### Data Splits

The dataset is organized into three batches:

- **Batch 1** (1,489 images): Fully annotated with JSON metadata and OCR text
- **Batch 2** (1,491 images): Images only, no annotations
- **Batch 3** (5,201 images): Images only, no annotations

**Total**: 8,181 invoice images

### Typical Image Characteristics

- **Format**: JPEG
- **Resolution**: ~1650 × 2300 pixels (portrait orientation)
- **File Size**: ~200-250 KB per image
- **Color**: RGB (3 channels)
- **Quality**: High-quality synthetic renders with clear text

## Dataset Creation

### Source Data

The images are synthetically generated invoices with realistic layouts including:
- Header with invoice number and date
- Seller and client information blocks with names, addresses, Tax IDs, and IBAN
- Itemized table with columns: No., Description, Qty, UM, Net price, Net worth, VAT [%], Gross worth
- Summary section with VAT breakdown and totals

#### Data Collection and Processing

This FiftyOne port was created by:
1. Downloading the original dataset from Kaggle
2. Parsing CSV annotations for Batch 1 (3 CSV files covering 1,489 images)
3. Extracting structured JSON and raw OCR text from the CSV
4. Creating FiftyOne samples with metadata for all 8,181 images
5. Computing image metadata (dimensions, file size, etc.)

### Annotations

#### Annotation Process

The 1,489 annotated samples include:
- **Structured JSON**: Extracted invoice fields organized hierarchically
- **Raw OCR Text**: Full text content from the invoice images

The annotations appear to be generated programmatically from the synthetic invoice creation process, ensuring high accuracy between the image content and the ground truth labels.

#### Personal and Sensitive Information

The dataset contains **synthetic data only**. Names, addresses, Tax IDs, IBANs, and other identifiers are fictionally generated and do not correspond to real individuals or organizations.

## Bias, Risks, and Limitations

### Known Limitations

- **Synthetic Data**: Invoices are artificially generated and may not capture the full diversity of real-world invoice formats
- **Single Language**: All invoices are in English
- **Uniform Layout**: All invoices follow a similar template structure
- **Limited Variety**: Invoice items appear to be e-commerce products with simple descriptions
- **Partial Annotations**: Only 18.2% (1,489/8,181) of images have ground truth annotations

### Recommendations

- Use this dataset for **initial model development** and **prototyping**
- **Augment with real invoice data** for production systems
- The unannotated images can be labeled using models trained on the annotated subset
- Consider fine-tuning on domain-specific invoices for production use cases
- Validate model performance on real invoices before deployment

## Citation

If you use this dataset, please cite:

**Original Dataset:**
```bibtex
@misc{high-quality-invoice-ocr,
  author = {Osama Hosam Abdellatif},
  title = {High Quality Invoice Images for OCR},
  year = {2024},
  publisher = {Kaggle},
  url = {https://www.kaggle.com/datasets/osamahosamabdellatif/high-quality-invoice-images-for-ocr}
}
```

**FiftyOne Port:**
```bibtex
@misc{high-quality-invoice-ocr-fiftyone,
  author = {Harpreet Sahota},
  title = {High Quality Invoice Images for OCR (FiftyOne)},
  year = {2026},
  publisher = {Hugging Face},
  url = {https://huggingface.co/datasets/harpreetsahota/high-quality-invoice-images-for-ocr}
}
```

## Dataset Card Contact

For questions or issues with this FiftyOne port, please open an issue on the Hugging Face dataset repository.