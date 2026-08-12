# KE-WP Dataset Usage Examples

This directory contains comprehensive examples demonstrating how to access, analyze, and visualize the Key Event to WikiPathways Mapping Dataset using different programming languages and tools.

## Files Overview

| File | Description | Target Audience |
|------|-------------|-----------------|
| `python_examples.py` | Complete Python examples with pandas, visualization | Data scientists, researchers |
| `r_examples.R` | R examples with dplyr, ggplot2, statistical analysis | R users, statisticians |
| `javascript_examples.js` | Frontend integration, API usage, interactive visualizations | Web developers |
| `index.html` | Interactive demo page showcasing JavaScript examples | General users, demos |
| `README.md` | This documentation file | All users |

## Quick Start

### Python Examples

```bash
# Install required packages
pip install requests pandas matplotlib seaborn pyarrow openpyxl

# Run examples
python python_examples.py
```

**Key Features:**
- Complete API client implementation
- Data export in multiple formats
- Pandas analysis and statistics
- Matplotlib/Seaborn visualizations
- Parquet integration for big data
- External API integration examples

### R Examples

```r
# Install required packages
install.packages(c("httr", "jsonlite", "dplyr", "ggplot2", "arrow", "plotly"))

# Run examples
source("r_examples.R")
```

**Key Features:**
- RESTful API client using httr
- Data analysis with dplyr
- ggplot2 visualizations
- Interactive plots with plotly
- Statistical analysis (chi-square tests)
- Correlation analysis

### JavaScript Examples

```html
<!-- Include in HTML page -->
<script src="javascript_examples.js"></script>
<script>runAllExamples();</script>
```

**Key Features:**
- Frontend API integration
- Interactive search interfaces
- Real-time statistics dashboards
- Chart.js visualizations
- Data export functionality
- External service integration

### Interactive Demo

Open `index.html` in a web browser to see a complete interactive demonstration of the dataset capabilities.

## Dataset Access Methods

### 1. REST API
```
Base URL: https://molaop-builder.vhp4safety.nl/api/v1/
```

**Core Endpoints:**
- `GET /mappings` - List mappings with filtering/pagination
- `GET /mappings/{id}` - Get specific mapping
- `GET /mappings/stats` - Dataset statistics
- `POST /mappings` - Create new mapping (auth required)

### 2. Data Export

**Working today** — these serve the current mapping state and are what the Downloads
page links to:

| What | URL |
|---|---|
| CSV of all mappings | `https://molaop-builder.vhp4safety.nl/download` |
| Gene sets, GMT | `/exports/gmt/ke-wp`, `/exports/gmt/ke-go`, `/exports/gmt/ke-reactome` (each also `-centric`) |
| Mappings with provenance, Turtle | `/exports/rdf/ke-wp`, `/exports/rdf/ke-go`, `/exports/rdf/ke-reactome` |
| Approved mappings, JSON | `/api/v1/mappings` (paginated) |

**Not yet available** — the generic `/export/{format}` route (and with it the Excel,
Parquet and JSON-LD formats) answers `500` because the metadata manager is unconfigured;
`/export/formats`, `/dataset/metadata` and `/dataset/datacite` likewise. Tracked in
[#160](https://github.com/marvinm2/molAOP-builder/issues/160). This section previously
listed those six formats as available, against a base URL that did not resolve either.

For bulk re-use prefer the Zenodo deposit, which bundles the GMT and Turtle exports per
resource and is citable: [10.5281/zenodo.20184643](https://doi.org/10.5281/zenodo.20184643).

### 3. Direct Database Access
- SQLite database file: `ke_wp_mapping.db`
- Tables: `mappings`, `proposals`, `dataset_versions`

## Common Use Cases

### Research Applications

#### 1. Pathway Enrichment Analysis
```python
# Get high-confidence causative mappings
client = KEWPDatasetClient()
mappings = client.getMappings({
    'confidence_level': 'high',
    'connection_type': 'causative',
    'per_page': 1000
})

# Extract pathway IDs for enrichment analysis
pathway_ids = [m['wp_id'] for m in mappings['data']]
```

#### 2. Temporal Trend Analysis
```r
# Analyze mapping creation trends
df <- client$get_all_mappings()
df$created_date <- as.Date(substr(df$created_at, 1, 10))

monthly_trends <- df %>%
  mutate(month = format(created_date, "%Y-%m")) %>%
  count(month, confidence_level) %>%
  ggplot(aes(x = month, y = n, color = confidence_level)) +
  geom_line(group = 1)
```

#### 3. Cross-Reference Validation
```javascript
// Validate mappings against external databases
const mappings = await client.getMappings({search: 'apoptosis'});
for (const mapping of mappings.data) {
    const keUrl = `https://aopwiki.org/events/${mapping.ke_id.replace('KE ', '')}`;
    const wpUrl = `https://www.wikipathways.org/pathways/${mapping.wp_id}.html`;
    // Perform cross-validation...
}
```

### Data Integration

#### 1. Merge with Experimental Data
```python
import pandas as pd

# Load dataset
mappings_df = pd.DataFrame(client.get_all_mappings())

# Merge with experimental results
experimental_df = pd.read_csv('experimental_results.csv')
merged_data = mappings_df.merge(
    experimental_df, 
    left_on='ke_id', 
    right_on='key_event_id'
)
```

#### 2. Network Analysis
```r
library(igraph)

# Create network from mappings
edges <- df %>% 
  select(ke_id, wp_id) %>%
  rename(from = ke_id, to = wp_id)

network <- graph_from_data_frame(edges, directed = TRUE)
plot(network, vertex.size = 5, edge.arrow.size = 0.3)
```

### Visualization Examples

#### 1. Confidence Distribution
```python
import matplotlib.pyplot as plt
import seaborn as sns

confidence_counts = df['confidence_level'].value_counts()
plt.pie(confidence_counts.values, labels=confidence_counts.index, autopct='%1.1f%%')
plt.title('Confidence Level Distribution')
plt.show()
```

#### 2. Interactive Dashboard
```javascript
// Real-time updating dashboard
async function updateDashboard() {
    const stats = await client.getStatistics();
    document.getElementById('totalMappings').textContent = 
        stats.data.summary.total_mappings.toLocaleString();
    
    // Update charts, tables, etc.
}
setInterval(updateDashboard, 60000); // Update every minute
```

## Advanced Usage

### Batch Processing
```python
# Process large datasets efficiently
def process_mappings_batch(batch_size=100):
    all_results = []
    page = 1
    
    while True:
        batch = client.get_mappings(page=page, per_page=batch_size)
        if not batch['data']:
            break
            
        # Process batch
        results = analyze_mapping_batch(batch['data'])
        all_results.extend(results)
        
        page += 1
        
    return all_results
```

### Caching and Performance
```javascript
class CachedKEWPClient extends KEWPDatasetClient {
    constructor(baseUrl, cacheTime = 5 * 60 * 1000) { // 5 minutes
        super(baseUrl);
        this.cache = new Map();
        this.cacheTime = cacheTime;
    }
    
    async getMappings(params = {}) {
        const key = JSON.stringify(params);
        const cached = this.cache.get(key);
        
        if (cached && Date.now() - cached.timestamp < this.cacheTime) {
            return cached.data;
        }
        
        const data = await super.getMappings(params);
        this.cache.set(key, { data, timestamp: Date.now() });
        return data;
    }
}
```

### Error Handling and Resilience
```python
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class ResilientKEWPClient(KEWPDatasetClient):
    def __init__(self, base_url="https://molaop-builder.vhp4safety.nl"):
        super().__init__(base_url)
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            method_whitelist=["GET"]
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
```

## Performance Tips

### 1. Efficient Querying
- Use pagination for large datasets
- Apply filters to reduce data transfer
- Cache frequently accessed data
- Use appropriate export formats (Parquet for analytics)

### 2. Rate Limit Management
- Default limits: 1000/hour general, 100/hour submissions
- Implement exponential backoff for retries
- Use bulk operations when available
- Consider authentication for higher limits

### 3. Data Processing
- Use streaming for large exports
- Leverage columnar formats (Parquet) for analysis
- Implement local caching for repeated queries
- Process data in batches to manage memory

## Integration Examples

### With Popular Tools

#### Jupyter Notebooks
```python
# Display interactive tables
from IPython.display import HTML
import json

mappings = client.get_all_mappings()
df = pd.DataFrame(mappings)

# Interactive display
HTML(df.to_html(escape=False, table_id="mappings-table"))
```

#### Streamlit Dashboard
```python
import streamlit as st

st.title("KE-WP Mapping Dashboard")

# Sidebar filters
confidence = st.sidebar.selectbox("Confidence Level", ["", "high", "medium", "low"])
connection = st.sidebar.selectbox("Connection Type", ["", "causative", "responsive"])

# Get filtered data
filters = {}
if confidence: filters['confidence_level'] = confidence
if connection: filters['connection_type'] = connection

data = client.get_mappings(**filters)
st.dataframe(pd.DataFrame(data['data']))
```

#### R Shiny Application
```r
library(shiny)

ui <- fluidPage(
  titlePanel("KE-WP Dataset Explorer"),
  sidebarLayout(
    sidebarPanel(
      selectInput("confidence", "Confidence Level:", 
                  choices = c("All" = "", "High" = "high", "Medium" = "medium", "Low" = "low")),
      selectInput("connection", "Connection Type:",
                  choices = c("All" = "", "Causative" = "causative", "Responsive" = "responsive"))
    ),
    mainPanel(
      DT::dataTableOutput("mappingsTable")
    )
  )
)

server <- function(input, output) {
  output$mappingsTable <- DT::renderDataTable({
    client <- KEWPClient$new()
    params <- list()
    if (input$confidence != "") params$confidence_level <- input$confidence
    if (input$connection != "") params$connection_type <- input$connection
    
    data <- client$get_mappings(!!!params)
    DT::datatable(data$data)
  })
}

shinyApp(ui = ui, server = server)
```

## 🐛 Troubleshooting

### Common Issues

1. **CORS Errors (JavaScript)**
   - Use a proxy server for development
   - Ensure proper CORS headers are set
   - Consider server-side API calls

2. **Rate Limiting**
   - Implement exponential backoff
   - Use authentication for higher limits
   - Cache responses locally

3. **Large Dataset Handling**
   - Use pagination for API calls
   - Stream data processing
   - Consider Parquet format for efficiency

4. **Missing Dependencies**
   - Check requirements files
   - Install optional dependencies as needed
   - Use virtual environments

### Getting Help

- **Documentation**: `/dataset/metadata` endpoint
- **API Reference**: `/api/v1/` endpoint
- **GitHub Issues**: Report bugs and request features
- **Community**: Join discussions and share examples

---

## Requirements

### Python
```
requests>=2.25.0
pandas>=1.3.0
matplotlib>=3.3.0
seaborn>=0.11.0
pyarrow>=5.0.0  # For Parquet support
openpyxl>=3.0.0  # For Excel export
```

### R
```
httr
jsonlite
dplyr
ggplot2
arrow  # For Parquet support
plotly  # For interactive plots
DT     # For interactive tables
corrplot
RColorBrewer
```

### JavaScript
```
# No specific requirements - vanilla JavaScript
# Optional: Chart.js for visualizations
# Optional: D3.js for advanced visualizations
```

## 📜 License

These examples are provided under the same license as the main project. The dataset itself is available under CC BY 4.0.

## Contributing

Found a bug or have an improvement idea? Please contribute!

1. Fork the repository
2. Add your example or fix
3. Test thoroughly
4. Submit a pull request

**Example Contribution Areas:**
- Additional language examples (Julia, MATLAB, etc.)
- Specialized analysis workflows
- Integration with other bioinformatics tools
- Performance optimizations
- Error handling improvements

---

*Last Updated: January 2025*  
*Examples Version: 1.0.0*