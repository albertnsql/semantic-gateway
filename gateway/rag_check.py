"""Quick confirmation script — run from gateway/ directory."""
import sys, os
sys.path.insert(0, ".")

chroma_dir = "./chroma_store"
populated = os.path.isdir(chroma_dir) and any(True for _ in os.scandir(chroma_dir))
print("=" * 60)
print("RAG CONFIRMATION CHECK")
print("=" * 60)
print(f"chroma_store/ exists and populated : {populated}")

if not populated:
    print("RAG STATUS: DISABLED (index missing — run: python -m rag.indexer)")
    sys.exit(0)

from rag.embedder import MetricEmbedder
e = MetricEmbedder(persist_dir=chroma_dir)
count = e.collection.count()
print(f"Metrics indexed in ChromaDB        : {count}")

q1 = "What is the MRR by plan type for the last 3 months?"
r1 = e.retrieve(q1, top_k=5)
print(f"\nQuery: '{q1}'")
print(f"  RAG top-5 : {[m['name'] for m in r1]}")

q2 = "Show me churn rate by country"
r2 = e.retrieve(q2, top_k=5)
print(f"\nQuery: '{q2}'")
print(f"  RAG top-5 : {[m['name'] for m in r2]}")

q3 = "engagement and recommendation metrics"
r3 = e.retrieve(q3, top_k=5)
print(f"\nQuery: '{q3}'")
print(f"  RAG top-5 : {[m['name'] for m in r3]}")

print("\n" + "=" * 60)
print("RAG STATUS: ACTIVE — only top-5 retrieved metrics go into the LLM prompt")
print("=" * 60)
