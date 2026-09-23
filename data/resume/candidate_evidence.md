# Candidate Evidence — Likhith Reddy Dosakayala

This file is used for JD matching evidence, not resume formatting.
The evaluator reads evidence only from `## Professional Experience` and `## Selected Projects`.
Source of truth: Likhith_Reddy_Resume_Flagship_2026 (facts locked 2026-08/09). Keep it honest —
anything not listed here counts as a gap when scoring a JD.

## Professional Experience

### H.A.D.E.S - Data Engineer (Nov 2024 - Aug 2025, remote)

- Sourced and cleaned Amazon product, review, and sales data for the top best-selling products in the India and US markets, covering competitor pricing, materials, and design choices for a new product launch.
- Built XGBoost and Learning-to-Rank models in Python to recommend materials and price points; validated the decision with structured A/B testing and reported findings via Power BI and Tableau dashboards.
- Built a RAG system (vector store plus LLM-based retrieval and generation) so stakeholders could query benchmarking findings in natural language.
- Built a propensity-score-matching causal inference pipeline using CLIP image embeddings as a product-quality confound control; covariate balance improved 74% and a placebo test validated the design.

### Hippocloud Technologies - Software Engineer (May 2022 - May 2023, India)

- Built a real-time data ingestion pipeline on GCP Pub/Sub and Dataflow (Apache Beam) in Python, processing 482K+ events from 4 sources with sub-60s latency and 99%+ reliability.
- Set up GitHub Actions CI/CD with unit and integration tests, Docker image builds, and Kubernetes deployments, plus structured logging and alerting, cutting MTTR by about 45%.
- Designed a hybrid MongoDB and PostgreSQL backend with schema partitioning and SQL index tuning, reducing API latency by about 40% for 10K+ daily requests.
- Evaluated XGBoost, Random Forest, and K-Means for customer segmentation and productionized XGBoost behind a Django REST Framework API serving 100K+ customer records at 87%+ F1-score.
- Built React frontend components on a Node.js and Express.js backend.

### Neo Aura Technologies - Junior Data Scientist (Jul 2023 - Aug 2024, India)

- Built ARIMA and SARIMAX time series forecasting models in Python over 121K commodity price records stored in MySQL, 23% lower error than baseline, deployed as a REST API serving 40 districts.
- Sourced knowledge-base data for a GPT-powered multilingual chatbot and owned its latency testing and benchmarking across 11 query types.

## Selected Projects

### FraudGuard-Agent - autonomous fraud-ops agent

- Kafka-streamed fraud detection pipeline with XGBoost (0.88 ROC-AUC) and an autonomous LLM agent with tool use that decides to act, retrain, or escalate per event; Docker, 22,088 transactions at 1.75ms average scoring latency; root-caused and fixed a distributed-consistency bug.

### AgentCascade - cost-aware LLM routing

- Cost-aware LLM task router using scikit-learn and gpt-4o-mini via the OpenAI API; cut real API spend 28% at identical accuracy (95.5%, ROC-AUC 0.9874); fixed a caching correctness bug with an exact-match cache; Kafka-based task flow.

### OrderPort Systems - canonical data model (GMU capstone, Product Owner)

- Led a 6-member team building a canonical data model and ETL to unify two POS systems: 317 products standardized, 10,064 customers deduplicated, Power BI dashboard over $5.96M revenue, 122 data-quality issues flagged, data governance roadmap.

### PortPulse - AWS streaming and forecasting pipeline

- AWS Kinesis, Firehose, S3, Glue, EMR with PySpark, and Redshift pipeline with SARIMAX forecasting and a Streamlit dashboard; 76 passing tests.

### PatchLoop - coding agent

- LLM coding agent evaluated on SWE-bench Lite with a controlled experiment on context-construction strategy.

### Additional skills with hands-on use

- Python, SQL, TypeScript, JavaScript, FastAPI, React, PyTorch, Hugging Face Transformers, LoRA and QLoRA fine-tuning, LLM-as-judge evaluation, Anthropic API, Snowflake, BigQuery, Airflow, dbt, Terraform, Prometheus, Grafana.
