import json
import os

import fire
import requests
from bs4 import BeautifulSoup, Comment

from r2r import (
    Document,
    EntityType,
    R2RAppBuilder,
    R2RConfig,
    Relation,
    format_entity_types,
    format_relations,
    generate_id_from_label,
)


def update_kg_prompt(
    prompt_provider,
    entity_types,
    relations,
    kg_extraction_prompt_name="ner_kg_extraction",
):
    # Fetch the kg extraction prompt with blank entity types and relations
    ner_kg_extraction_with_spec = prompt_provider.get_prompt(
        "ner_kg_extraction_with_spec"
    )

    # Format the prompt to include the desired entity types and relations
    ner_kg_extraction = ner_kg_extraction_with_spec.replace(
        "{entity_types}", format_entity_types(entity_types)
    ).replace("{relations}", format_relations(relations))

    # Update the "ner_kg_extraction" prompt used in downstream KG construction
    prompt_provider.update_prompt(
        kg_extraction_prompt_name,
        json.dumps(ner_kg_extraction, ensure_ascii=False),
    )


def get_all_yc_co_directory_urls():
    this_file_path = os.path.abspath(os.path.dirname(__file__))
    yc_company_dump_path = os.path.join(
        this_file_path, "..", "data", "yc_companies.txt"
    )

    with open(yc_company_dump_path, "r") as f:
        urls = f.readlines()
    urls = [url.strip() for url in urls]
    return {url.split("/")[-1]: url for url in urls}


# Function to fetch and clean HTML content
def fetch_and_clean_yc_co_data(url):
    # Fetch the HTML content from the URL
    response = requests.get(url)
    response.raise_for_status()  # Raise an error for bad status codes
    html_content = response.text

    # Parse the HTML content with BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove all <script>, <style>, <meta>, <link>, <header>, <nav>, and <footer> elements
    for element in soup(
        ["script", "style", "meta", "link", "header", "nav", "footer"]
    ):
        element.decompose()

    # Remove comments
    for comment in soup.findAll(text=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Select the main content (you can adjust the selector based on the structure of your target pages)
    main_content = soup.select_one("main") or soup.body

    if main_content:
        spans = main_content.find_all(["span", "a"])

        proc_spans = []
        for span in spans:
            proc_spans.append(span.get_text(separator=" ", strip=True))
        span_text = "\n".join(proc_spans)

        # Extract the text content from the main content
        paragraphs = main_content.find_all(
            ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]
        )
        cleaned_text = (
            "### Bulk:\n\n"
            + "\n\n".join(
                paragraph.get_text(separator=" ", strip=True)
                for paragraph in paragraphs
            )
            + "\n\n### Metadata:\n\n"
            + span_text
        )

        return cleaned_text
    else:
        return "Main content not found"


def execute_query(provider, query, params={}):
    print(f"Executing query: {query}")
    with provider.client.session(database=provider._database) as session:
        result = session.run(query, params)
        return [record.data() for record in result]


def delete_all_entries(provider):
    delete_query = "MATCH (n) DETACH DELETE n;"
    with provider.client.session(database=provider._database) as session:
        session.run(delete_query)
    print("All entries deleted.")


def print_all_relationships(provider):
    rel_query = """
    MATCH (n1)-[r]->(n2)
    RETURN n1.id AS subject, type(r) AS relation, n2.id AS object;
    """
    with provider.client.session(database=provider._database) as session:
        results = session.run(rel_query)
        for record in results:
            print(
                f"{record['subject']} -[{record['relation']}]-> {record['object']}"
            )


def main(max_entries=50, delete=False):
    # Load the R2R configuration and build the app
    this_file_path = os.path.abspath(os.path.dirname(__file__))
    config_path = os.path.join(
        this_file_path, "..", "configs", "neo4j_kg.json"
    )
    config = R2RConfig.from_json(config_path)
    r2r = R2RAppBuilder(config).build()

    # Get the providers
    kg_provider = r2r.providers.kg
    prompt_provider = r2r.providers.prompt

    # Specify the entity types for the KG extraction prompt
    entity_types = [
        EntityType(
            "ORGANIZATION",
            subcategories=["COMPANY", "SCHOOL", "NON-PROFIT", "OTHER"],
        ),
        EntityType(
            "LOCATION", subcategories=["CITY", "STATE", "COUNTRY", "OTHER"]
        ),
        EntityType("PERSON"),
        EntityType("POSITION"),
        EntityType(
            "DATE",
            subcategories=[
                "YEAR",
                "MONTH",
                "DAY",
                "BATCH (E.G. W24, S20)",
                "OTHER",
            ],
        ),
        EntityType("QUANTITY"),
        EntityType(
            "EVENT",
            subcategories=[
                "INCORPORATION",
                "FUNDING_ROUND",
                "ACQUISITION",
                "LAUNCH",
                "OTHER",
            ],
        ),
        EntityType("INDUSTRY"),
        EntityType(
            "MEDIA",
            subcategories=["EMAIL", "WEBSITE", "TWITTER", "LINKEDIN", "OTHER"],
        ),
        EntityType("PRODUCT"),
    ]

    # Specify the relations for the KG construction
    relations = [
        # Founder Relations
        Relation("EDUCATED_AT"),
        Relation("WORKED_AT"),
        Relation("FOUNDED"),
        # Company relations
        Relation("RAISED"),
        Relation("REVENUE"),
        Relation("TEAM_SIZE"),
        Relation("LOCATION"),
        Relation("ACQUIRED_BY"),
        Relation("ANNOUNCED"),
        Relation("INDUSTRY"),
        # Product relations
        Relation("PRODUCT"),
        Relation("FEATURES"),
        Relation("USES"),
        Relation("USED_BY"),
        Relation("TECHNOLOGY"),
        # Additional relations
        Relation("HAS"),
        Relation("AS_OF"),
        Relation("PARTICIPATED"),
        Relation("ASSOCIATED"),
        Relation("GROUP_PARTNER"),
        Relation("ALIAS"),
    ]

    # Update the KG extraction prompt with the specified entity types and relations
    update_kg_prompt(prompt_provider, entity_types, relations)

    # Optional - clear the graph if the delete flag is set
    if delete:
        delete_all_entries(kg_provider)

    url_map = get_all_yc_co_directory_urls()

    i = 0
    # Ingest and clean the data for each company
    for company, url in url_map.items():
        company_data = fetch_and_clean_yc_co_data(url)
        if i >= max_entries:
            break
        try:
            # Ingest as a text document
            r2r.ingest_documents(
                [
                    Document(
                        id=generate_id_from_label(company),
                        type="txt",
                        data=company_data,
                        metadata={},
                    )
                ]
            )
        except:
            continue
        i += 1

    print_all_relationships(kg_provider)


if __name__ == "__main__":
    fire.Fire(main)
