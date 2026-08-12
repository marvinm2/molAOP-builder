"""
Python Examples for KE-WP Mapping Dataset
Demonstrates various ways to access and analyze the dataset
"""

import requests
import pandas as pd
import json
from typing import Dict, List
import matplotlib.pyplot as plt


class KEWPDatasetClient:
    """Client for accessing KE-WP Mapping Dataset API"""
    
    def __init__(self, base_url: str = "https://molaop-builder.vhp4safety.nl"):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
    
    def get_mappings(self, **kwargs) -> Dict:
        """Get mappings with optional filtering and pagination"""
        response = self.session.get(f"{self.base_url}/api/v1/mappings", params=kwargs)
        response.raise_for_status()
        return response.json()
    
    def get_mapping(self, mapping_id: int) -> Dict:
        """Get specific mapping by ID"""
        response = self.session.get(f"{self.base_url}/api/v1/mappings/{mapping_id}")
        response.raise_for_status()
        return response.json()
    
    def search_mappings(self, query: str, **kwargs) -> Dict:
        """Search mappings by text query"""
        params = {"search": query, **kwargs}
        return self.get_mappings(**params)
    
    def get_statistics(self) -> Dict:
        """Get dataset statistics"""
        response = self.session.get(f"{self.base_url}/api/v1/mappings/stats")
        response.raise_for_status()
        return response.json()
    
    def export_data(self, format_name: str, **options) -> requests.Response:
        """Export dataset in specified format"""
        response = self.session.get(f"{self.base_url}/export/{format_name}", params=options)
        response.raise_for_status()
        return response
    
    def get_all_mappings(self, per_page: int = 100) -> List[Dict]:
        """Get all mappings by paginating through results"""
        all_mappings = []
        page = 1
        
        while True:
            data = self.get_mappings(page=page, per_page=per_page)
            mappings = data["data"]
            all_mappings.extend(mappings)
            
            if not data["pagination"]["has_next"]:
                break
            page += 1
        
        return all_mappings


def basic_data_access():
    """Example 1: Basic data access and exploration"""
    print("=== Example 1: Basic Data Access ===")
    
    client = KEWPDatasetClient()
    
    # Get first page of mappings
    data = client.get_mappings(per_page=20)
    mappings = data["data"]
    pagination = data["pagination"]
    
    print(f"Retrieved {len(mappings)} mappings (page 1 of {pagination['pages']})")
    print(f"Total mappings in dataset: {pagination['total']}")
    
    # Display first few mappings
    for i, mapping in enumerate(mappings[:3]):
        print(f"\nMapping {i+1}:")
        print(f"  KE: {mapping['ke_id']} - {mapping['ke_title']}")
        print(f"  WP: {mapping['wp_id']} - {mapping['wp_title']}")
        print(f"  Confidence: {mapping['confidence_level']}")
        print(f"  Connection: {mapping['connection_type']}")


def filtering_and_search():
    """Example 2: Filtering and search functionality"""
    print("\n=== Example 2: Filtering and Search ===")
    
    client = KEWPDatasetClient()
    
    # Search for oxidative stress related mappings
    oxidative_results = client.search_mappings("oxidative stress", per_page=10)
    print(f"Found {len(oxidative_results['data'])} mappings related to 'oxidative stress'")
    
    # Filter by high confidence causative relationships
    high_conf_results = client.get_mappings(
        confidence_level="high", 
        connection_type="causative",
        per_page=20
    )
    print(f"Found {len(high_conf_results['data'])} high-confidence causative mappings")
    
    # Filter by specific Key Event
    ke_results = client.get_mappings(ke_id="KE 1234", per_page=10)
    print(f"Found {len(ke_results['data'])} mappings for KE 1234")


def data_export_examples():
    """Example 3: Data export in different formats"""
    print("\n=== Example 3: Data Export ===")
    
    client = KEWPDatasetClient()
    
    # Export as JSON
    json_response = client.export_data("json", metadata="true", provenance="true")
    json_data = json_response.json()
    print(f"JSON export contains {len(json_data['mappings'])} mappings")
    
    # Save JSON to file
    with open("ke_wp_dataset.json", "w") as f:
        json.dump(json_data, f, indent=2)
    print("Saved comprehensive JSON export to 'ke_wp_dataset.json'")
    
    # Export as Parquet (binary format)
    parquet_response = client.export_data("parquet", include_metadata_columns="true")
    with open("ke_wp_dataset.parquet", "wb") as f:
        f.write(parquet_response.content)
    print("Saved Parquet export to 'ke_wp_dataset.parquet'")
    
    # Export as Excel
    excel_response = client.export_data("excel", include_statistics="true")
    with open("ke_wp_dataset.xlsx", "wb") as f:
        f.write(excel_response.content)
    print("Saved Excel workbook to 'ke_wp_dataset.xlsx'")


def pandas_analysis():
    """Example 4: Data analysis with pandas"""
    print("\n=== Example 4: Pandas Analysis ===")
    
    client = KEWPDatasetClient()
    
    # Get all data and convert to DataFrame
    all_mappings = client.get_all_mappings()
    df = pd.DataFrame(all_mappings)
    
    print(f"Dataset shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")
    
    # Basic statistics
    print("\nConfidence Level Distribution:")
    confidence_dist = df['confidence_level'].value_counts()
    print(confidence_dist)
    
    print("\nConnection Type Distribution:")
    connection_dist = df['connection_type'].value_counts()
    print(connection_dist)
    
    # Top contributors
    print("\nTop 10 Contributors:")
    top_contributors = df['created_by'].value_counts().head(10)
    print(top_contributors)
    
    # Temporal analysis
    df['created_at'] = pd.to_datetime(df['created_at'])
    df['created_month'] = df['created_at'].dt.to_period('M')
    
    print("\nMonthly Creation Counts:")
    monthly_counts = df['created_month'].value_counts().sort_index()
    print(monthly_counts.head())


def visualization_examples():
    """Example 5: Data visualization"""
    print("\n=== Example 5: Data Visualization ===")
    
    client = KEWPDatasetClient()
    
    # Get statistics from API
    stats = client.get_statistics()
    data = stats["data"]
    
    # Set up plotting
    plt.style.use('seaborn-v0_8')
    fig, axes = plt.subplots(2, 2, figsize=(15, 10))
    fig.suptitle('KE-WP Mapping Dataset Statistics', fontsize=16)
    
    # Confidence level distribution
    conf_data = data["distributions"]["confidence_levels"]
    axes[0, 0].pie(conf_data.values(), labels=conf_data.keys(), autopct='%1.1f%%')
    axes[0, 0].set_title('Confidence Level Distribution')
    
    # Connection type distribution
    conn_data = data["distributions"]["connection_types"]
    axes[0, 1].bar(conn_data.keys(), conn_data.values())
    axes[0, 1].set_title('Connection Type Distribution')
    axes[0, 1].set_ylabel('Count')
    
    # Top contributors
    contrib_data = data["distributions"]["contributors"]
    top_10_contrib = dict(list(contrib_data.items())[:10])
    axes[1, 0].barh(list(top_10_contrib.keys()), list(top_10_contrib.values()))
    axes[1, 0].set_title('Top 10 Contributors')
    axes[1, 0].set_xlabel('Contributions')
    
    # Temporal distribution
    temporal_data = data["distributions"]["temporal"]
    months = list(temporal_data.keys())
    counts = list(temporal_data.values())
    axes[1, 1].plot(months, counts, marker='o')
    axes[1, 1].set_title('Temporal Distribution')
    axes[1, 1].set_ylabel('Mappings Created')
    axes[1, 1].tick_params(axis='x', rotation=45)
    
    plt.tight_layout()
    plt.savefig('ke_wp_statistics.png', dpi=300, bbox_inches='tight')
    print("Saved visualization to 'ke_wp_statistics.png'")
    plt.show()


def advanced_analysis():
    """Example 6: Advanced analysis techniques"""
    print("\n=== Example 6: Advanced Analysis ===")
    
    client = KEWPDatasetClient()
    
    # Load data with Parquet for better performance
    try:
        import pyarrow.parquet as pq  # noqa: F401 — availability probe for the example block
        
        # Download Parquet file
        parquet_response = client.export_data("parquet", include_metadata_columns="true")
        with open("temp_dataset.parquet", "wb") as f:
            f.write(parquet_response.content)
        
        # Read with arrow for better performance
        df = pd.read_parquet("temp_dataset.parquet")
        
        print(f"Loaded {len(df)} mappings with enhanced analytics columns")
        print(f"Analytics columns: {[col for col in df.columns if 'numeric' in col or 'length' in col]}")
        
        # Correlation analysis
        numeric_cols = ['confidence_numeric', 'connection_numeric', 'ke_title_length', 'wp_title_length']
        correlation_matrix = df[numeric_cols].corr()
        
        print("\nCorrelation Matrix:")
        print(correlation_matrix)
        
        # Confidence vs connection type analysis
        confidence_connection = pd.crosstab(df['confidence_level'], df['connection_type'], normalize='index')
        print("\nConfidence vs Connection Type (Normalized):")
        print(confidence_connection)
        
    except ImportError:
        print("PyArrow not installed. Skipping advanced Parquet analysis.")
        print("Install with: pip install pyarrow")


def integration_example():
    """Example 7: Integration with external tools"""
    print("\n=== Example 7: Integration Example ===")
    
    client = KEWPDatasetClient()
    
    # Get mappings for a specific biological process
    apoptosis_mappings = client.search_mappings("apoptosis", per_page=50)
    
    print(f"Found {len(apoptosis_mappings['data'])} apoptosis-related mappings")
    
    # Extract unique pathway IDs for external analysis
    pathway_ids = set()
    for mapping in apoptosis_mappings['data']:
        pathway_ids.add(mapping['wp_id'])
    
    print(f"Unique pathways involved: {len(pathway_ids)}")
    print("Sample pathway URLs for external tools:")
    
    for wp_id in list(pathway_ids)[:5]:
        print(f"  - WikiPathways: https://www.wikipathways.org/pathways/{wp_id}.html")
        print(f"  - SVG Diagram: https://www.wikipathways.org/wikipathways-assets/pathways/{wp_id}/{wp_id}.svg")


def main():
    """Run all examples"""
    print("KE-WP Mapping Dataset - Python Examples")
    print("=" * 50)
    
    try:
        basic_data_access()
        filtering_and_search()
        data_export_examples()
        pandas_analysis()
        visualization_examples()
        advanced_analysis()
        integration_example()
        
        print("\n" + "=" * 50)
        print("All examples completed successfully!")
        print("Check the generated files:")
        print("- ke_wp_dataset.json (comprehensive JSON export)")
        print("- ke_wp_dataset.parquet (analytics-ready format)")  
        print("- ke_wp_dataset.xlsx (Excel workbook with multiple sheets)")
        print("- ke_wp_statistics.png (visualization)")
        
    except requests.exceptions.RequestException as e:
        print(f"Network error: {e}")
        print("Make sure the KE-WP mapping service is running and accessible")
    except Exception as e:
        print(f"Error running examples: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()