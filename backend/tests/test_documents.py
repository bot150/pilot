import io
import unittest
from unittest.mock import patch

from docx import Document as WordDocument
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.chat_routes import chat_router
from app.config import Settings, get_settings
from app.database import Base, get_db
from app.document_routes import document_router
from app.models import DocumentMetadata
from app.services.hindsight import get_hindsight_adapter


class DocumentUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.calls: list[tuple[str, object]] = []
        self.retained_items: list[dict[str, str]] = []

        class FakeHindsight:
            async def retain_many(inner_self, items):
                self.calls.append(("retain_many", items))
                self.retained_items.extend(items)
                return {"success": True, "items_count": len(items)}

            async def recall(inner_self, **kwargs):
                self.calls.append(("recall", kwargs))
                return {"results": [{"id": str(index), "text": item["content"], "context": item["context"], "document_id": item["document_id"]} for index, item in enumerate(self.retained_items)]}

        def db_session():
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        self.app = FastAPI()
        self.app.include_router(document_router)
        self.app.include_router(chat_router)
        self.app.dependency_overrides[get_db] = db_session
        self.app.dependency_overrides[get_hindsight_adapter] = lambda: FakeHindsight()
        self.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, openai_api_key="test-key")
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _make_docx(self) -> bytes:
        document = WordDocument()
        document.add_paragraph("Project requirements: support a saved shopping cart and recommendations.")
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()

    def _make_pdf(self) -> bytes:
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
        })
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 72 220 Td (PDF extraction example content.) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    def test_txt_upload_retains_extracted_text_and_only_stores_metadata(self) -> None:
        response = self.client.post(
            "/api/documents/upload",
            files={"file": ("ecommerce_project.txt", b"The project includes a recommendation engine.", "text/plain")},
        )

        self.assertEqual(response.status_code, 201, response.text)
        document = response.json()["document"]
        self.assertEqual(document["filename"], "ecommerce_project.txt")
        self.assertEqual(document["status"], "ready")
        self.assertEqual(document["memory_count"], 1)
        operation, items = self.calls[0]
        self.assertEqual(operation, "retain_many")
        self.assertIn("recommendation engine", items[0]["content"])
        self.assertEqual(items[0]["document_id"], document["id"])
        self.assertIn("ecommerce_project.txt", items[0]["context"])

        listed = self.client.get("/api/documents")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()[0]["id"], document["id"])
        self.assertNotIn("content", listed.json()[0])
        with self.session_factory() as session:
            stored = session.get(DocumentMetadata, document["id"])
            self.assertIsNotNone(stored)
            self.assertFalse(hasattr(stored, "content"))

    def test_docx_and_csv_are_extracted(self) -> None:
        word_response = self.client.post(
            "/api/documents/upload",
            files={"file": ("requirements.docx", self._make_docx(), "application/octet-stream")},
        )
        csv_response = self.client.post(
            "/api/documents/upload",
            files={"file": ("catalog.csv", b"name,category\nWidget,Home\n", "text/csv")},
        )

        self.assertEqual(word_response.status_code, 201, word_response.text)
        self.assertEqual(csv_response.status_code, 201, csv_response.text)
        all_contents = "\n".join(item["content"] for _, batch in self.calls for item in batch)
        self.assertIn("saved shopping cart", all_contents)
        self.assertIn("Widget | Home", all_contents)

    def test_pdf_text_is_extracted_before_retaining(self) -> None:
        response = self.client.post(
            "/api/documents/upload",
            files={"file": ("product_brief.pdf", self._make_pdf(), "application/pdf")},
        )

        self.assertEqual(response.status_code, 201, response.text)
        items = self.calls[0][1]
        self.assertIn("PDF extraction example content", items[0]["content"])

    def test_document_detail_recall_is_scoped_by_document_metadata(self) -> None:
        uploaded = self.client.post(
            "/api/documents/upload",
            files={"file": ("guide.txt", b"A useful project guide.", "text/plain")},
        )
        document_id = uploaded.json()["document"]["id"]

        response = self.client.get(f"/api/documents/{document_id}/memories")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["document"]["filename"], "guide.txt")
        self.assertEqual(response.json()["memories"][0]["text"], "A useful project guide.")
        self.assertEqual(self.calls[-1][0], "recall")
        self.assertIn(document_id, self.calls[-1][1]["query"])

    def test_upload_then_chat_recalls_document_context_for_ai_generation(self) -> None:
        uploaded = self.client.post(
            "/api/documents/upload",
            files={"file": ("ecommerce_project.txt", b"The document lists a product catalog, personalized recommendations, and order tracking.", "text/plain")},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        generated: list[dict] = []

        async def generate(**kwargs):
            generated.append(kwargs)
            self.calls.append(("generate", kwargs))
            return "The document lists a product catalog, personalized recommendations, and order tracking."

        with patch("app.chat_routes.generate_chat_response", side_effect=generate):
            answer = self.client.post(
                "/api/chat",
                json={"message": "What are the main features mentioned in the document?"},
            )

        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIn("personalized recommendations", answer.json()["answer"])
        self.assertEqual([name for name, _ in self.calls[-2:]], ["recall", "generate"])
        self.assertIn("ecommerce_project.txt", generated[0]["memories"][0]["context"])
        self.assertIn("product catalog", generated[0]["memories"][0]["text"])

    def test_invalid_types_malformed_documents_and_oversized_uploads_are_rejected(self) -> None:
        unsupported = self.client.post(
            "/api/documents/upload",
            files={"file": ("script.exe", b"not executable", "application/octet-stream")},
        )
        spoofed_pdf = self.client.post(
            "/api/documents/upload",
            files={"file": ("document.pdf", b"not a PDF", "application/pdf")},
        )
        oversized = self.client.post(
            "/api/documents/upload",
            files={"file": ("large.txt", b"x" * (10 * 1024 * 1024 + 1), "text/plain")},
        )

        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(spoofed_pdf.status_code, 415)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(self.calls, [])

    def test_filename_path_is_reduced_to_a_safe_label(self) -> None:
        response = self.client.post(
            "/api/documents/upload",
            files={"file": ("..\\private\\notes.txt", b"A document with a safe display name.", "text/plain")},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["document"]["filename"], "notes.txt")


if __name__ == "__main__":
    unittest.main()
