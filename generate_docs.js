import fs from 'fs';
import {
    Document, Packer, Paragraph, TextRun, HeadingLevel, TableOfContents,
    Table, TableRow, TableCell, BorderStyle, WidthType, AlignmentType,
    PageBreak, Header, Footer, PageNumber
} from 'docx';

const bodyStyle = {
    font: "Arial",
    size: 22, // 11pt = 22 half-points
    lineHeight: 1.15 * 240 // Assuming 240 is default
};

const codeStyle = {
    font: "Courier New",
    size: 20, // 10pt
};

const p = (text, options = {}) => {
    return new Paragraph({
        children: [
            new TextRun({
                text: text,
                font: "Arial",
                size: 22,
                ...options
            })
        ],
        spacing: { line: 276, before: 120, after: 120 } // 1.15 line spacing ~ 276
    });
};

const h1 = (text) => {
    return new Paragraph({
        text: text,
        heading: HeadingLevel.HEADING_1,
    });
};

const h2 = (text) => {
    return new Paragraph({
        text: text,
        heading: HeadingLevel.HEADING_2,
    });
};

const tableHeaderCell = (text) => {
    return new TableCell({
        children: [new Paragraph({ children: [new TextRun({ text, font: "Arial", size: 22, bold: true })] })],
        shading: { fill: "E6F7F6" },
        margins: { top: 100, bottom: 100, left: 100, right: 100 }
    });
};

const tableBodyCell = (text, isAlternate, isCode = false) => {
    const run = isCode ? new TextRun({ text, font: "Courier New", size: 20 }) : new TextRun({ text, font: "Arial", size: 22 });
    return new TableCell({
        children: [new Paragraph({ children: [run] })],
        shading: { fill: isAlternate ? "F5FAFA" : "FFFFFF" },
        margins: { top: 100, bottom: 100, left: 100, right: 100 }
    });
};

const defaultBorders = {
    top: { style: BorderStyle.SINGLE, size: 1, color: "000000" },
    bottom: { style: BorderStyle.SINGLE, size: 1, color: "000000" },
    left: { style: BorderStyle.SINGLE, size: 1, color: "000000" },
    right: { style: BorderStyle.SINGLE, size: 1, color: "000000" },
    insideHorizontal: { style: BorderStyle.SINGLE, size: 1, color: "000000" },
    insideVertical: { style: BorderStyle.SINGLE, size: 1, color: "000000" },
};

const doc = new Document({
    styles: {
        paragraphStyles: [
            {
                id: "Heading1",
                name: "Heading 1",
                basedOn: "Normal",
                next: "Normal",
                quickFormat: true,
                run: {
                    font: "Arial",
                    size: 36, // 18pt
                    bold: true,
                },
                paragraph: {
                    spacing: { before: 240, after: 120 },
                },
            },
            {
                id: "Heading2",
                name: "Heading 2",
                basedOn: "Normal",
                next: "Normal",
                quickFormat: true,
                run: {
                    font: "Arial",
                    size: 28, // 14pt
                    bold: true,
                },
                paragraph: {
                    spacing: { before: 240, after: 120 },
                },
            },
        ],
    },
    sections: [
        {
            properties: {
                page: {
                    size: {
                        width: 12240, // 8.5 inches
                        height: 15840 // 11 inches
                    },
                    margin: {
                        top: 1440,
                        right: 1440,
                        bottom: 1440,
                        left: 1440,
                    },
                },
            },
            headers: {
                default: new Header({
                    children: [
                        new Paragraph({
                            alignment: AlignmentType.RIGHT,
                            children: [
                                new TextRun({
                                    text: "SemanticGateway — Technical Documentation",
                                    font: "Arial",
                                    size: 22,
                                    color: "888888"
                                }),
                            ],
                        }),
                    ],
                }),
            },
            footers: {
                default: new Footer({
                    children: [
                        new Paragraph({
                            alignment: AlignmentType.CENTER,
                            children: [
                                new TextRun({
                                    children: [PageNumber.CURRENT],
                                    font: "Arial",
                                    size: 22,
                                    color: "888888"
                                }),
                            ],
                        }),
                    ],
                }),
            },
            children: [
                // 1. COVER PAGE
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { before: 2000, after: 400 },
                    children: [
                        new TextRun({ text: "SemanticGateway", font: "Arial", size: 56, bold: true })
                    ]
                }),
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { after: 800 },
                    children: [
                        new TextRun({ text: "AI-Native Semantic Layer for Governed Analytics", font: "Arial", size: 32, italics: true })
                    ]
                }),
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { after: 800 },
                    children: [
                        new TextRun({ text: "A governed semantic layer sitting between natural language queries and a Snowflake warehouse to prevent hallucinated joins and grain violations.", font: "Arial", size: 24 })
                    ]
                }),
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { after: 2000 },
                    children: [
                        new TextRun({ text: "React, FastAPI, dbt MetricFlow, Snowflake, OpenAI/Claude", font: "Arial", size: 20, color: "555555" })
                    ]
                }),
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { after: 200 },
                    children: [
                        new TextRun({ text: "Antigravity", font: "Arial", size: 24 })
                    ]
                }),
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    children: [
                        new TextRun({ text: new Date().toLocaleDateString(), font: "Arial", size: 24 })
                    ]
                }),
                new Paragraph({ children: [new PageBreak()] }),

                // 2. TABLE OF CONTENTS
                h1("2. TABLE OF CONTENTS"),
                new TableOfContents("Table of Contents", {
                    hyperlink: true,
                    headingStyleRange: "1-2",
                }),
                new Paragraph({ children: [new PageBreak()] }),

                // 3. EXECUTIVE SUMMARY
                h1("3. EXECUTIVE SUMMARY"),
                p("SemanticGateway solves the critical problem of unconstrained AI agents querying enterprise data by introducing a strict, governed semantic layer. Rather than allowing a Large Language Model to write ad-hoc SQL against raw tables—which inevitably leads to hallucinated joins, ignored business logic, and mixed grains—this application forces all natural language questions through a certified MetricFlow registry. Data teams and analytics engineers can use it to expose complex warehouse data to business users safely, ensuring that every answer is mathematically sound and conceptually accurate. As a portfolio project, it demonstrates a deep understanding of modern data engineering architecture, bridging the gap between raw generative AI capabilities and the rigorous data governance required in production environments."),

                // 4. TECH STACK & ARCHITECTURE OVERVIEW
                h1("4. TECH STACK & ARCHITECTURE OVERVIEW"),
                new Table({
                    width: { size: 100, type: WidthType.PERCENTAGE },
                    borders: defaultBorders,
                    rows: [
                        new TableRow({
                            children: [
                                tableHeaderCell("Technology"),
                                tableHeaderCell("Role"),
                                tableHeaderCell("Why")
                            ]
                        }),
                        new TableRow({
                            children: [
                                tableBodyCell("React (Vite)", false),
                                tableBodyCell("Frontend UI", false),
                                tableBodyCell("Provides a fast, responsive, and component-driven interface with a modern Claymorphism aesthetic.", false)
                            ]
                        }),
                        new TableRow({
                            children: [
                                tableBodyCell("FastAPI", true),
                                tableBodyCell("Backend API", true),
                                tableBodyCell("High-performance Python framework ideal for orchestrating LLM calls and managing synchronous semantic model parsing.", true)
                            ]
                        }),
                        new TableRow({
                            children: [
                                tableBodyCell("dbt MetricFlow", false),
                                tableBodyCell("Semantic Layer", false),
                                tableBodyCell("Acts as the single source of truth for metric definitions, enforcing strict governance over dimensions and grains.", false)
                            ]
                        }),
                        new TableRow({
                            children: [
                                tableBodyCell("Snowflake", true),
                                tableBodyCell("Data Warehouse", true),
                                tableBodyCell("Highly scalable cloud warehouse capable of handling the complex analytical queries generated by MetricFlow.", true)
                            ]
                        }),
                        new TableRow({
                            children: [
                                tableBodyCell("OpenAI (gpt-4o)", false),
                                tableBodyCell("Intent Extraction", false),
                                tableBodyCell("Translates natural language questions into a structured JSON intent constrained by the certified metric registry.", false)
                            ]
                        })
                    ]
                }),
                p("The overall architecture decouples the natural language processing from the actual SQL generation. When a user submits a query through the React frontend, the FastAPI backend intercepts it and passes it to the LLM along with a strict vocabulary of certified metrics and dimensions. The LLM acts purely as an Intent Extractor, outputting a structured JSON payload. This intent is then strictly validated against the MetricRegistry. If it passes, it is handed off to the MetricFlow CLI, which dynamically compiles the governed SQL and executes it against Snowflake. Finally, the results, along with the generated SQL and data lineage, are returned to the UI."),

                // 5. PROJECT STRUCTURE
                h1("5. PROJECT STRUCTURE"),
                p("frontend/src/"),
                p("├── api/              # API clients for querying the FastAPI backend", { font: "Courier New", size: 20 }),
                p("├── assets/           # Static assets and images", { font: "Courier New", size: 20 }),
                p("├── components/       # Reusable React components (KpiCard, Sidebar, etc.)", { font: "Courier New", size: 20 }),
                p("│   ├── dashboard/    # Specialized components for the dashboard view", { font: "Courier New", size: 20 }),
                p("│   └── ui/           # Generic UI components (Buttons, Badges)", { font: "Courier New", size: 20 }),
                p("├── hooks/            # Custom React hooks", { font: "Courier New", size: 20 }),
                p("├── pages/            # Top-level route components representing distinct views", { font: "Courier New", size: 20 }),
                p("├── App.jsx           # Root layout, routing configuration, and global state", { font: "Courier New", size: 20 }),
                p("├── index.css         # Global Tailwind CSS and custom claymorphism styles", { font: "Courier New", size: 20 }),
                p("└── main.jsx          # Application entry point", { font: "Courier New", size: 20 }),
                p("The project structure clearly separates presentation components from page-level layouts and API abstraction logic. The dedicated dashboard subdirectory within components organizes complex, domain-specific UI elements away from generic shared components."),

                // 6. FEATURE WALKTHROUGH
                h1("6. FEATURE WALKTHROUGH — PAGE BY PAGE"),
                h2("Landing Page"),
                p("The Landing Page serves as the entry point and executive overview of the application. It outlines the core value proposition of the SemanticGateway, detailing how it prevents hallucinated joins and grain violations while guaranteeing certified metrics and lineage tracing. It renders a visually engaging pipeline diagram mapping the architecture from raw SaaS data to the React frontend. It performs no external API calls but provides immediate navigation links to the application's core tools."),
                h2("Dashboard Page"),
                p("The Dashboard Page provides an executive-level summary of key performance indicators and trends. It renders a grid of KPI tiles (MRR, active subscribers, churn rate) alongside complex Recharts-powered data visualizations. It fetches real-time data using concurrent API calls to the /api/v1/dashboard endpoint, applying a local cache with a 5-minute TTL to optimize performance. A built-in chat panel allows users to ask ad-hoc questions directly within the dashboard context, integrating the semantic query engine into the reporting flow."),
                h2("Query Interface"),
                p("The Query Interface is the primary interactive testing ground for natural language analytics. It renders a structured input form where users can submit questions and toggle options to include SQL generation details or lineage paths. It sends the user's query to the backend via a POST request and handles various response states, rendering a sophisticated QueryResultPanel upon success. Notably, it includes specialized error handling for missing LLM API keys and gracefully renders clarification cards if the semantic validation fails."),
                h2("Metrics Catalog"),
                p("The Metrics Catalog acts as the front-facing data dictionary. It fetches the certified metric registry from the backend and renders each metric as an expandable card. Users can see the exact definition, source model, grain, and certified dimensions for every metric. This page is purely informational and relies on the GET /api/v1/metrics endpoint to populate its state, reinforcing the concept of a governed, transparent semantic layer."),
                h2("Lineage Explorer"),
                p("The Lineage Explorer visualizes the upstream data transformations for any given metric. Users select a metric from a dropdown, triggering a fetch to the backend to retrieve the lineage path. The page renders a custom LineageGraph component mapping the journey from raw tables through staging and marts models up to the final metric. This provides crucial auditability and trust for data consumers."),
                h2("Demo Scenarios"),
                p("The Demo Scenarios page provides one-click executions of predefined queries to demonstrate the gateway's capabilities. It renders three specific use cases: a valid query, a query designed to trigger a grain mismatch rejection, and a lineage trace. Each scenario independently calls the query API and displays the resulting QueryResultPanel, serving as an interactive tutorial for new users."),

                // 7. HOW THE APP WORKS
                h1("7. HOW THE APP WORKS — CORE FLOWS"),
                p("Flow 1 — Natural Language Query:"),
                p("1. The user types a question into the QueryPage form and submits it."),
                p("2. The frontend sends a POST request to the /api/v1/query backend route."),
                p("3. The FastAPI route invokes the IntentExtractor, which injects the certified metric registry into the system prompt and calls the LLM."),
                p("4. The LLM returns a structured JSON intent specifying the desired metrics, dimensions, and time grains."),
                p("5. The backend SemanticValidator checks the intent against the registry to ensure the grain and dimensions are valid. If validation fails, it intercepts the query and returns a clarification response."),
                p("6. If valid, the SqlGenerator leverages the dbt MetricFlow CLI to compile the semantic query into governed SQL."),
                p("7. The Snowflake connection executes the generated SQL and returns the raw data."),
                p("8. The backend packages the data, SQL, and lineage into a unified response payload."),
                p("9. The React frontend renders the results dynamically in the QueryResultPanel."),
                p("Flow 2 — Metrics Catalog Browse:"),
                p("1. The user navigates to the Metrics Catalog route."),
                p("2. The MetricsCatalogPage component mounts and triggers a GET request to /api/v1/metrics."),
                p("3. The backend MetricRegistry parses the underlying dbt semantic YAML files synchronously, extracting dimensions, grains, and descriptions."),
                p("4. The backend returns an array of certified MetricDefinition objects."),
                p("5. The frontend iterates over this array, rendering an expandable MetricCard for each entity, accurately reflecting the semantic layer's structure."),

                // 8. SETUP & INSTALLATION GUIDE
                h1("8. SETUP & INSTALLATION GUIDE"),
                p("Prerequisites: Node.js 18+, Python 3.10+, and an active Snowflake account."),
                p("1. Clone the repository and navigate to the project root."),
                p("2. Navigate to the frontend directory: cd frontend"),
                p("3. Install frontend dependencies: npm install"),
                p("4. Start the Vite development server: npm run dev"),
                p("5. Open a new terminal and navigate to the backend directory: cd gateway"),
                p("6. Install Python dependencies: pip install -r requirements.txt"),
                p("7. Copy the environment template: cp .env.example .env"),
                p("8. Configure the environment variables in .env (see table below)."),
                p("9. Start the FastAPI server: uvicorn api.main:app --reload"),
                new Table({
                    width: { size: 100, type: WidthType.PERCENTAGE },
                    borders: defaultBorders,
                    rows: [
                        new TableRow({ children: [tableHeaderCell("Variable"), tableHeaderCell("Description"), tableHeaderCell("Example")] }),
                        new TableRow({ children: [tableBodyCell("OPENAI_API_KEY", false, true), tableBodyCell("API key for the LLM", false), tableBodyCell("gsk_...", false, true)] }),
                        new TableRow({ children: [tableBodyCell("SNOWFLAKE_ACCOUNT", true, true), tableBodyCell("Snowflake account URL", true), tableBodyCell("xyz.snowflakecomputing.com", true, true)] }),
                        new TableRow({ children: [tableBodyCell("SNOWFLAKE_USER", false, true), tableBodyCell("Snowflake username", false), tableBodyCell("admin", false, true)] }),
                        new TableRow({ children: [tableBodyCell("SNOWFLAKE_PASSWORD", true, true), tableBodyCell("Snowflake password", true), tableBodyCell("secure_pass", true, true)] }),
                        new TableRow({ children: [tableBodyCell("MANIFEST_PATH", false, true), tableBodyCell("Path to dbt manifest.json", false), tableBodyCell("../dbt_streaming_analytics/...", false, true)] })
                    ]
                }),
                p("Common setup error: If the frontend reports a 401 Unauthorized or Intent Extraction Failed, verify that OPENAI_API_KEY is correctly set in the gateway/.env file and that the backend has been restarted."),

                // 9. API REFERENCE
                h1("9. API REFERENCE"),
                new Table({
                    width: { size: 100, type: WidthType.PERCENTAGE },
                    borders: defaultBorders,
                    rows: [
                        new TableRow({ children: [tableHeaderCell("Method"), tableHeaderCell("Endpoint"), tableHeaderCell("Description"), tableHeaderCell("Request Body"), tableHeaderCell("Response Shape")] }),
                        new TableRow({ children: [tableBodyCell("GET", false, true), tableBodyCell("/api/v1/metrics/health", false, true), tableBodyCell("Checks backend and MetricFlow status", false), tableBodyCell("None", false), tableBodyCell('{"status": "healthy"}', false, true)] }),
                        new TableRow({ children: [tableBodyCell("GET", true, true), tableBodyCell("/api/v1/metrics", true, true), tableBodyCell("Returns all certified metrics", true), tableBodyCell("None", true), tableBodyCell('[{"name": "mrr", "dimensions": [...]}]', true, true)] }),
                        new TableRow({ children: [tableBodyCell("GET", false, true), tableBodyCell("/api/v1/metrics/{name}/lineage", false, true), tableBodyCell("Returns lineage for a metric", false), tableBodyCell("None", false), tableBodyCell('{"metric_name": "mrr", "source_tables": [...]}', false, true)] }),
                        new TableRow({ children: [tableBodyCell("POST", true, true), tableBodyCell("/api/v1/query", true, true), tableBodyCell("Executes a natural language query", true), tableBodyCell('{"query": "...", "history": [...]}', true, true), tableBodyCell('{"status": "success", "data": [...]}', true, true)] })
                    ]
                }),

                // 10. KEY ENGINEERING DECISIONS
                h1("10. KEY ENGINEERING DECISIONS"),
                p("I chose to implement dbt MetricFlow as the semantic layer rather than building a custom SQL generator. While building a custom solution would have allowed for faster initial development and fewer dependencies, it would have inevitably led to the classic LLM hallucination problems—inventing columns and performing fan-out joins. By delegating the SQL compilation to MetricFlow, I ensured that every query is mathematically and structurally sound, guaranteeing trust in the data."),
                p("To enforce grain safety and prevent hallucination, I designed a strict two-step pipeline. The LLM is isolated from the warehouse schema entirely; it acts only as an Intent Extractor, selecting from a hardcoded list of certified metrics and time grains. I considered letting the LLM write direct SQL and then parsing it for safety, but parsing arbitrary SQL is fragile. My approach of generating a JSON intent and validating it against the MetricRegistry guarantees that cross-grain calculations are blocked before compilation even begins."),
                p("For the frontend architecture, I utilized React with Vite, managing state primarily through isolated component state and custom hooks. I avoided heavy global state management libraries like Redux, as the application's data flow is predominantly unidirectional—fetching and displaying semantic models and query results. This keeps the bundle size small and the component lifecycle predictable."),
                p("I selected Snowflake as the underlying data warehouse due to its robust integration with dbt and its ability to effortlessly scale compute resources. Alternative open-source engines like PostgreSQL would have sufficed for a prototype, but Snowflake represents the realistic target environment for enterprise semantic layers, allowing me to demonstrate production-grade integration patterns."),

                // 11. KNOWN LIMITATIONS & FUTURE ROADMAP
                h1("11. KNOWN LIMITATIONS & FUTURE ROADMAP"),
                p("The application currently lacks user authentication and role-based access control; any user who can reach the frontend can execute queries against the warehouse. Furthermore, the frontend dashboard currently falls back to displaying hardcoded mock data if the backend API fails to return the expected payload, masking potential underlying query failures. Finally, the MetricRegistry parsing is synchronous and tied to file system reads upon startup, which may scale poorly if the dbt project grows to thousands of models."),
                p("Phase 2 Roadmap:"),
                p("1. Implement OAuth2 authentication and user-level data masking policies."),
                p("2. Add an interactive data-exploration UI to allow point-and-click querying without natural language."),
                p("3. Integrate Redis caching on the backend to reduce redundant MetricFlow compilation times."),
                p("4. Introduce WebSocket streaming for long-running warehouse queries."),
                p("5. Expand the chat panel to support conversational memory across sessions."),
                p("6. Build an admin view to dynamically reload the dbt manifest without restarting the server."),

                // 12. GLOSSARY
                h1("12. GLOSSARY"),
                p("Semantic Layer: A governed abstraction layer (powered by dbt MetricFlow in this app) that sits between the raw warehouse tables and the user, ensuring consistency in how business concepts are calculated."),
                p("MetricFlow: The specific tool used by the backend to compile user intent into safe, validated SQL based on predefined YAML definitions."),
                p("Grain: The level of granularity of a dataset. In this app, the gateway enforces grain safety to ensure monthly subscription data isn't erroneously joined with session-level event data."),
                p("Metric: A certified, mathematically defined business calculation (e.g., MRR) registered within the application's semantic models."),
                p("Dimension: An approved attribute by which a metric can be sliced (e.g., plan_type, period_month). The gateway prevents the LLM from inventing unauthorized dimensions."),
                p("Natural Language Query: The unstructured text input provided by the user, which the Intent Extractor translates into a structured JSON payload."),
                p("Hallucinated Join: A common error in AI data tools where an LLM writes SQL connecting two unrelated tables, leading to inaccurate row counts. This app's architecture prevents this entirely."),
                p("Lineage: The documented path showing exactly how raw source tables were transformed through staging and intermediate layers to produce a final metric."),
                p("dbt: The data build tool used to define the underlying transformations and semantic models that power the gateway's registry.")
            ],
        },
    ],
});

Packer.toBuffer(doc).then((buffer) => {
    fs.writeFileSync("C:\\mnt\\user-data\\outputs\\SemanticGateway_Documentation.docx", buffer);
    console.log("Document created successfully");
});
