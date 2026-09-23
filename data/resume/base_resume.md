# Likhith Reddy Dosakayala

Fairfax, VA  
[linkedin.com/in/likhith-reddy-d-69a1412a5](https://linkedin.com/in/likhith-reddy-d-69a1412a5)  
[github.com/likithreddy25](https://github.com/likithreddy25)

## Summary

Data / ML / software engineer with 2.5+ years of experience across data engineering, machine learning, and backend development. Hands-on with Python, SQL, real-time pipelines (GCP Pub/Sub, Dataflow, Kafka), XGBoost and time series modeling, and GenAI systems (RAG, agent tool use, cost-aware LLM routing). M.S. Data Analytics, George Mason University (May 2026).

## Technical Skills

**Languages:** Python, SQL, TypeScript, JavaScript  
**ML & Statistical Modeling:** scikit-learn, XGBoost, Random Forest, K-Means, ARIMA/SARIMAX, Learning-to-Rank, causal inference (PSM), CLIP embeddings, PyTorch, Hugging Face Transformers, LoRA/QLoRA  
**GenAI & Agents:** OpenAI and Anthropic APIs, RAG, agent tool-use loops, LLM-as-judge evaluation, prompt/model benchmarking  
**Backend & Full Stack:** FastAPI, Django REST Framework, REST API design, React, Node.js/Express.js  
**Data & Streaming:** Kafka, PySpark, Apache Beam, PostgreSQL, MongoDB, MySQL  
**Cloud:** AWS (Kinesis, Firehose, S3, Glue, EMR, Redshift), GCP (Pub/Sub, Dataflow, BigQuery), Snowflake  
**DevOps & MLOps:** Docker, Kubernetes, Terraform, GitHub Actions CI/CD, Airflow, dbt, Prometheus, Grafana  
**Analytics:** Power BI, Tableau, A/B testing, statistical analysis

## Professional Experience

### Data Engineer — H.A.D.E.S (Nov 2024 – Aug 2025, Remote)

- Sourced and cleaned Amazon product, review, and sales data for top-selling products in the India and US markets to benchmark competitor pricing, materials, and design.
- Built XGBoost and Learning-to-Rank models to recommend materials and a $30–35 target price; validated with A/B testing and reported via Power BI and Tableau.
- Built a RAG system over the sourced data so stakeholders could query findings in natural language.
- Built a propensity-score-matching pipeline with CLIP image embeddings as a quality confound control; balance improved 74% (max SMD 0.68 → 0.18), placebo test passed.

### Software Engineer — Hippocloud Technologies (May 2022 – May 2023, India)

- Built a real-time ingestion pipeline on GCP Pub/Sub and Dataflow processing 482K+ events with sub-60s latency and 99%+ reliability.
- Set up GitHub Actions CI/CD (tests, Docker builds, Kubernetes deploys) plus structured logging and alerting, cutting MTTR ~45%.
- Designed a hybrid MongoDB/PostgreSQL backend with index tuning, reducing API latency ~40% across 10K+ daily requests.
- Productionized an XGBoost segmentation model behind a Django REST Framework API over 100K+ customer records (87%+ F1).

### Junior Data Scientist — Neo Aura Technologies (Jul 2023 – Aug 2024, India)

- Built ARIMA/SARIMAX forecasting over 121K commodity records (MySQL), 23% lower error than baseline, deployed as a REST API serving 40 districts.
- Sourced knowledge-base data for a Telugu/English GPT chatbot and owned latency benchmarking across 11 query types.

## Selected Projects

- **FraudGuard-Agent** — Kafka-streamed XGBoost fraud detection (0.88 ROC-AUC) with an autonomous LLM agent that acts, retrains, or escalates; 1.75ms average scoring latency over 22,088 transactions.
- **AgentCascade** — cost-aware LLM task router; cut real API spend 28% at identical accuracy (95.5%, ROC-AUC 0.9874).
- **OrderPort Systems** — Product Owner for a 6-member GMU capstone; canonical data model over two POS systems, 10,064 customers deduplicated, Power BI dashboard over $5.96M revenue.

## Education

- M.S. Data Analytics — George Mason University, Fairfax, VA (May 2026), GPA 3.7/4.0
- B.E. Electronics and Computer Engineering — REVA University (May 2023)
