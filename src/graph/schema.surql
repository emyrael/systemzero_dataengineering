-- E-commerce knowledge graph. Design and rationale: docs/GRAPH_MODEL.md
--
-- Record ids are the source business keys (product:⟨PRD-000001⟩, purchased:⟨TXN-…⟩), so a
-- reload upserts instead of duplicating. Every record carries `batch_id`, the pipeline run
-- that last wrote it; records a run did not write are pruned after a successful load.
-- Applied on every load (OVERWRITE), so schema changes need no migration step.

-- ══ nodes ═══════════════════════════════════════════════════════════════════════

DEFINE TABLE OVERWRITE vendor SCHEMAFULL COMMENT 'Supplier; features from gold/vendor_features';
DEFINE FIELD OVERWRITE name ON vendor TYPE string;
DEFINE FIELD OVERWRITE status ON vendor TYPE option<string>;
DEFINE FIELD OVERWRITE country ON vendor TYPE option<string>;
DEFINE FIELD OVERWRITE region ON vendor TYPE option<string>;
DEFINE FIELD OVERWRITE reliability_score ON vendor TYPE option<float>;
DEFINE FIELD OVERWRITE lead_time_days ON vendor TYPE option<int>;
DEFINE FIELD OVERWRITE payment_terms ON vendor TYPE option<string>;
DEFINE FIELD OVERWRITE certifications ON vendor TYPE array<string>;
DEFINE FIELD OVERWRITE total_products_supplied ON vendor TYPE int;
DEFINE FIELD OVERWRITE revenue_generated ON vendor TYPE float;
DEFINE FIELD OVERWRITE product_quality_score ON vendor TYPE option<float>;
DEFINE FIELD OVERWRITE invoice_payment_rate ON vendor TYPE option<float>;
DEFINE FIELD OVERWRITE average_invoice_value ON vendor TYPE option<float>;
DEFINE FIELD OVERWRITE total_outstanding_balance ON vendor TYPE float;
DEFINE FIELD OVERWRITE batch_id ON vendor TYPE string;

DEFINE TABLE OVERWRITE product SCHEMAFULL COMMENT 'Catalogue product; features from gold/product_features';
DEFINE FIELD OVERWRITE name ON product TYPE string;
DEFINE FIELD OVERWRITE sku ON product TYPE option<string>;
DEFINE FIELD OVERWRITE description ON product TYPE option<string>;
DEFINE FIELD OVERWRITE category ON product TYPE option<string>;
DEFINE FIELD OVERWRITE subcategory ON product TYPE option<string>;
DEFINE FIELD OVERWRITE tags ON product TYPE array<string>;
DEFINE FIELD OVERWRITE price ON product TYPE option<float>;
DEFINE FIELD OVERWRITE cost ON product TYPE option<float>;
DEFINE FIELD OVERWRITE rating ON product TYPE option<float>;
DEFINE FIELD OVERWRITE stock_quantity ON product TYPE option<int>;
DEFINE FIELD OVERWRITE is_active ON product TYPE option<bool>;
DEFINE FIELD OVERWRITE units_sold ON product TYPE int;
DEFINE FIELD OVERWRITE revenue_contribution ON product TYPE float;
DEFINE FIELD OVERWRITE revenue_share_pct ON product TYPE float;
DEFINE FIELD OVERWRITE velocity_score ON product TYPE float;
DEFINE FIELD OVERWRITE stock_turnover_rate ON product TYPE option<float>;
DEFINE FIELD OVERWRITE price_tier ON product TYPE option<string>;
DEFINE FIELD OVERWRITE vendor_reliability_weighted_score ON product TYPE option<float>;
DEFINE FIELD OVERWRITE dq_flags ON product TYPE array<string>;
DEFINE FIELD OVERWRITE batch_id ON product TYPE string;

-- No email, phone or street address: the graph serves analytics and GraphRAG, which need
-- behaviour, not contact details (data minimisation). Silver keeps them.
DEFINE TABLE OVERWRITE customer SCHEMAFULL COMMENT 'Customer; features from gold/customer_features';
DEFINE FIELD OVERWRITE name ON customer TYPE string;
DEFINE FIELD OVERWRITE segment ON customer TYPE option<string>;
DEFINE FIELD OVERWRITE gender ON customer TYPE option<string>;
DEFINE FIELD OVERWRITE age ON customer TYPE option<int>;
DEFINE FIELD OVERWRITE city ON customer TYPE option<string>;
DEFINE FIELD OVERWRITE is_active ON customer TYPE option<bool>;
DEFINE FIELD OVERWRITE registration_date ON customer TYPE option<datetime>;
DEFINE FIELD OVERWRITE total_orders ON customer TYPE int;
DEFINE FIELD OVERWRITE total_spend ON customer TYPE float;
DEFINE FIELD OVERWRITE tenure_months ON customer TYPE float;
DEFINE FIELD OVERWRITE purchase_frequency ON customer TYPE float;
DEFINE FIELD OVERWRITE average_order_value ON customer TYPE option<float>;
DEFINE FIELD OVERWRITE customer_lifetime_value ON customer TYPE float;
DEFINE FIELD OVERWRITE days_since_last_purchase ON customer TYPE option<int>;
DEFINE FIELD OVERWRITE customer_segment_score ON customer TYPE option<int>;
DEFINE FIELD OVERWRITE rfm_segment ON customer TYPE string;
DEFINE FIELD OVERWRITE dq_flags ON customer TYPE array<string>;
DEFINE FIELD OVERWRITE batch_id ON customer TYPE string;

DEFINE TABLE OVERWRITE category SCHEMAFULL COMMENT 'Product category';
DEFINE FIELD OVERWRITE name ON category TYPE string;
DEFINE FIELD OVERWRITE batch_id ON category TYPE string;

DEFINE TABLE OVERWRITE country SCHEMAFULL COMMENT 'Country of a customer or vendor';
DEFINE FIELD OVERWRITE name ON country TYPE string;
DEFINE FIELD OVERWRITE batch_id ON country TYPE string;

DEFINE TABLE OVERWRITE region SCHEMAFULL COMMENT 'Sales region (config reference.country_regions)';
DEFINE FIELD OVERWRITE name ON region TYPE string;
DEFINE FIELD OVERWRITE batch_id ON region TYPE string;

DEFINE TABLE OVERWRITE invoice SCHEMAFULL COMMENT 'Vendor invoice; features from gold/invoice_features';
DEFINE FIELD OVERWRITE number ON invoice TYPE option<string>;
DEFINE FIELD OVERWRITE invoice_date ON invoice TYPE datetime;
DEFINE FIELD OVERWRITE due_date ON invoice TYPE option<datetime>;
DEFINE FIELD OVERWRITE payment_terms ON invoice TYPE option<string>;
DEFINE FIELD OVERWRITE payment_status ON invoice TYPE option<string>;
DEFINE FIELD OVERWRITE payment_date ON invoice TYPE option<datetime>;
DEFINE FIELD OVERWRITE subtotal ON invoice TYPE option<float>;
DEFINE FIELD OVERWRITE tax_amount ON invoice TYPE option<float>;
DEFINE FIELD OVERWRITE total_amount ON invoice TYPE option<float>;
DEFINE FIELD OVERWRITE amount_paid ON invoice TYPE option<float>;
DEFINE FIELD OVERWRITE balance_due ON invoice TYPE option<float>;
DEFINE FIELD OVERWRITE is_credit_note ON invoice TYPE bool;
DEFINE FIELD OVERWRITE days_to_payment ON invoice TYPE option<int>;
DEFINE FIELD OVERWRITE days_overdue ON invoice TYPE option<int>;
DEFINE FIELD OVERWRITE line_item_diversity ON invoice TYPE int;
DEFINE FIELD OVERWRITE discount_rate_achieved ON invoice TYPE option<float>;
DEFINE FIELD OVERWRITE reconciliation_flag ON invoice TYPE option<bool>;
DEFINE FIELD OVERWRITE reconciliation_note ON invoice TYPE string;
DEFINE FIELD OVERWRITE dq_flags ON invoice TYPE array<string>;
DEFINE FIELD OVERWRITE batch_id ON invoice TYPE string;
DEFINE INDEX OVERWRITE invoice_status ON invoice FIELDS payment_status;

-- Text nodes: unstructured context for GraphRAG, linked to the entities they mention.
DEFINE TABLE OVERWRITE support_ticket SCHEMAFULL COMMENT 'Support ticket with summary and transcript text';
DEFINE FIELD OVERWRITE channel ON support_ticket TYPE option<string>;
DEFINE FIELD OVERWRITE category ON support_ticket TYPE option<string>;
DEFINE FIELD OVERWRITE priority ON support_ticket TYPE option<string>;
DEFINE FIELD OVERWRITE status ON support_ticket TYPE option<string>;
DEFINE FIELD OVERWRITE created_at ON support_ticket TYPE datetime;
DEFINE FIELD OVERWRITE resolved_at ON support_ticket TYPE option<datetime>;
DEFINE FIELD OVERWRITE satisfaction_score ON support_ticket TYPE option<int>;
DEFINE FIELD OVERWRITE resolution_type ON support_ticket TYPE option<string>;
DEFINE FIELD OVERWRITE summary ON support_ticket TYPE option<string>;
DEFINE FIELD OVERWRITE transcript ON support_ticket TYPE option<string>;
DEFINE FIELD OVERWRITE tags ON support_ticket TYPE array<string>;
DEFINE FIELD OVERWRITE batch_id ON support_ticket TYPE string;

DEFINE TABLE OVERWRITE review SCHEMAFULL COMMENT 'Product review text';
DEFINE FIELD OVERWRITE rating ON review TYPE option<int>;
DEFINE FIELD OVERWRITE title ON review TYPE option<string>;
DEFINE FIELD OVERWRITE text ON review TYPE option<string>;
DEFINE FIELD OVERWRITE sentiment ON review TYPE option<string>;
DEFINE FIELD OVERWRITE verified_purchase ON review TYPE option<bool>;
DEFINE FIELD OVERWRITE helpful_votes ON review TYPE option<int>;
DEFINE FIELD OVERWRITE review_date ON review TYPE option<datetime>;
DEFINE FIELD OVERWRITE response_text ON review TYPE option<string>;
DEFINE FIELD OVERWRITE dq_flags ON review TYPE array<string>;
DEFINE FIELD OVERWRITE batch_id ON review TYPE string;

DEFINE TABLE OVERWRITE call SCHEMAFULL COMMENT 'Support call; transcript is the utterances as "speaker: text" lines';
DEFINE FIELD OVERWRITE call_start ON call TYPE datetime;
DEFINE FIELD OVERWRITE call_end ON call TYPE option<datetime>;
DEFINE FIELD OVERWRITE duration_seconds ON call TYPE option<int>;
DEFINE FIELD OVERWRITE call_type ON call TYPE option<string>;
DEFINE FIELD OVERWRITE language ON call TYPE option<string>;
DEFINE FIELD OVERWRITE agent_name ON call TYPE option<string>;
DEFINE FIELD OVERWRITE sentiment ON call TYPE option<string>;
DEFINE FIELD OVERWRITE sentiment_start ON call TYPE option<string>;
DEFINE FIELD OVERWRITE sentiment_end ON call TYPE option<string>;
DEFINE FIELD OVERWRITE resolution_achieved ON call TYPE option<bool>;
DEFINE FIELD OVERWRITE quality_score ON call TYPE option<int>;
DEFINE FIELD OVERWRITE keywords ON call TYPE array<string>;
DEFINE FIELD OVERWRITE transcript ON call TYPE string;
DEFINE FIELD OVERWRITE batch_id ON call TYPE string;

-- ══ edges ═══════════════════════════════════════════════════════════════════════

DEFINE TABLE OVERWRITE supplies TYPE RELATION FROM vendor TO product SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON supplies TYPE string;

DEFINE TABLE OVERWRITE belongs_to TYPE RELATION FROM product TO category SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON belongs_to TYPE string;

DEFINE TABLE OVERWRITE located_in TYPE RELATION FROM customer | vendor TO country SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON located_in TYPE string;

DEFINE TABLE OVERWRITE part_of TYPE RELATION FROM country TO region SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON part_of TYPE string;

-- One edge per transaction. `recognised` marks revenue (captured payment, order not
-- cancelled), the same rule as the Gold features, so graph and feature totals agree.
DEFINE TABLE OVERWRITE purchased TYPE RELATION FROM customer TO product SCHEMAFULL;
DEFINE FIELD OVERWRITE order_id ON purchased TYPE string;
DEFINE FIELD OVERWRITE transaction_date ON purchased TYPE datetime;
DEFINE FIELD OVERWRITE quantity ON purchased TYPE int;
DEFINE FIELD OVERWRITE unit_price ON purchased TYPE float;
DEFINE FIELD OVERWRITE net_revenue ON purchased TYPE float;
DEFINE FIELD OVERWRITE total_amount ON purchased TYPE float;
DEFINE FIELD OVERWRITE is_return ON purchased TYPE bool;
DEFINE FIELD OVERWRITE recognised ON purchased TYPE bool;
DEFINE FIELD OVERWRITE payment_status ON purchased TYPE option<string>;
DEFINE FIELD OVERWRITE order_status ON purchased TYPE option<string>;
DEFINE FIELD OVERWRITE channel ON purchased TYPE option<string>;
DEFINE FIELD OVERWRITE sales_region ON purchased TYPE option<string>;
DEFINE FIELD OVERWRITE batch_id ON purchased TYPE string;
DEFINE INDEX OVERWRITE purchased_date ON purchased FIELDS transaction_date;

DEFINE TABLE OVERWRITE billed TYPE RELATION FROM vendor TO invoice SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON billed TYPE string;

DEFINE TABLE OVERWRITE invoice_item TYPE RELATION FROM invoice TO product SCHEMAFULL;
DEFINE FIELD OVERWRITE line_number ON invoice_item TYPE int;
DEFINE FIELD OVERWRITE quantity ON invoice_item TYPE int;
DEFINE FIELD OVERWRITE unit_cost ON invoice_item TYPE float;
DEFINE FIELD OVERWRITE line_total ON invoice_item TYPE float;
DEFINE FIELD OVERWRITE is_credit ON invoice_item TYPE bool;
DEFINE FIELD OVERWRITE batch_id ON invoice_item TYPE string;

-- Co-purchase: products bought by the same customers (recognised purchases). Stored in
-- both directions so `->similar_to` works from either product.
DEFINE TABLE OVERWRITE similar_to TYPE RELATION FROM product TO product SCHEMAFULL;
DEFINE FIELD OVERWRITE shared_customers ON similar_to TYPE int;
DEFINE FIELD OVERWRITE batch_id ON similar_to TYPE string;

DEFINE TABLE OVERWRITE opened TYPE RELATION FROM customer TO support_ticket SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON opened TYPE string;

DEFINE TABLE OVERWRITE wrote TYPE RELATION FROM customer TO review SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON wrote TYPE string;

DEFINE TABLE OVERWRITE about TYPE RELATION FROM support_ticket | review TO product SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON about TYPE string;

DEFINE TABLE OVERWRITE has_call TYPE RELATION FROM support_ticket TO call SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON has_call TYPE string;

DEFINE TABLE OVERWRITE called TYPE RELATION FROM customer TO call SCHEMAFULL;
DEFINE FIELD OVERWRITE batch_id ON called TYPE string;
