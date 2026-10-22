from __future__ import annotations

import time

from sentence_transformers import SentenceTransformer

try:
    import config
except ImportError:
    from . import config


def load_model() -> SentenceTransformer:
    try:
        model = SentenceTransformer(
            config.EMBEDDING_MODEL,
            trust_remote_code=True,
            device="cpu",
            local_files_only=True,
        )
    except Exception:
        print("Downloading embedding model for throughput audit...")
        model = SentenceTransformer(
            config.EMBEDDING_MODEL,
            trust_remote_code=True,
            device="cpu",
        )

    model.max_seq_length = 512
    return model


def main() -> None:
    model = load_model()
    texts = [
        (
            f"search_document: This is a test document number {i} "
            f"with some realistic content about invoices and meetings "
            f"and downloads and screenshots. " * 3
        ).strip()
        for i in range(128)
    ]

    for batch_size in [1, 4, 8, 16, 32, 64]:
        batches = [texts[index : index + batch_size] for index in range(0, len(texts), batch_size)]
        started_at = time.perf_counter()
        for batch in batches:
            model.encode(
                batch,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        elapsed = time.perf_counter() - started_at
        per_doc_ms = (elapsed / len(texts)) * 1000 if texts else 0.0
        print(
            f"batch_size={batch_size:3d}  "
            f"total={elapsed:.2f}s  "
            f"per_doc={per_doc_ms:.1f}ms"
        )


if __name__ == "__main__":
    main()
