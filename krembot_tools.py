import json
import neo4j
import pyodbc
import requests
import xml.etree.ElementTree as ET

from langchain.chains.query_constructor.base import AttributeInfo
from langchain.retrievers.self_query.base import SelfQueryRetriever
from langchain_community.vectorstores import Pinecone as LangPine
from langchain_openai import OpenAIEmbeddings
from langchain_openai.chat_models import ChatOpenAI

from openai import OpenAI
import os
from os import getenv
from pinecone import Pinecone
from pinecone_text.sparse import BM25Encoder
from typing import List, Dict, Any, Tuple, Union, Optional
from krembot_db import work_prompts

mprompts = work_prompts()
client = OpenAI(api_key=getenv("OPENAI_API_KEY"))


def connect_to_neo4j() -> neo4j.Driver:
    """
    Establishes a connection to the Neo4j database using credentials from environment variables.

    Returns:
        neo4j.Driver: A Neo4j driver instance for interacting with the database.
    """
    uri = getenv("NEO4J_URI")
    user = getenv("NEO4J_USER")
    password = getenv("NEO4J_PASS")
    return neo4j.GraphDatabase.driver(uri, auth=(user, password))


def connect_to_pinecone(x: int) -> Any:
    """
    Connects to a Pinecone index based on the provided parameter.

    Args:
        x (int): Determines which Pinecone host to connect to. If x is 0, connects to the primary host;
                 otherwise, connects to the secondary host.

    Returns:
        Any: An instance of Pinecone Index connected to the specified host.
    """
    pinecone_api_key = getenv('PINECONE_API_KEY')
    pinecone_host = (
        "https://delfi-a9w1e6k.svc.aped-4627-b74a.pinecone.io"
        if x == 0
        else "https://neo-positive-a9w1e6k.svc.apw5-4e34-81fa.pinecone.io"
    )
    pinecone_client = Pinecone(api_key=pinecone_api_key, host=pinecone_host)
    return pinecone_client.Index(host=pinecone_host)


def rag_tool_answer(prompt: str, x: int) -> Tuple[Any, str]:
    """
    Generates an answer using the RAG (Retrieval-Augmented Generation) tool based on the provided prompt and context.

    The function behavior varies depending on the 'APP_ID' environment variable. It utilizes different processors
    and tools to fetch and generate the appropriate response.

    Args:
        prompt (str): The input query or prompt for which an answer is to be generated.
        x (int): Additional parameter that may influence the processing logic, such as device selection.

    Returns:
        Tuple[Any, str]: A tuple containing the generated context or search results and the RAG tool used.
    """
    rag_tool = "ClientDirect"
    app_id = os.getenv("APP_ID")

    if app_id == "InteliBot":
        return intelisale(prompt), rag_tool

    elif app_id == "DentyBot":
        processor = HybridQueryProcessor(namespace="denty-serviser", delfi_special=1)
        search_results = processor.process_query_results(upit=prompt, device=x)
        return search_results, rag_tool

    elif app_id == "DentyBotS":
        processor = HybridQueryProcessor(namespace="denty-komercijalista", delfi_special=1)
        context = processor.process_query_results(prompt)
        return context, rag_tool

    elif app_id == "ECDBot":
        processor = HybridQueryProcessor(namespace="ecd", delfi_special=1)
        return processor.process_query_results(prompt), rag_tool

    context = " "
    rag_tool = get_structured_decision_from_model(prompt)

    if rag_tool == "Hybrid":
        processor = HybridQueryProcessor(namespace="delfi-podrska", delfi_special=1)
        context = processor.process_query_results(prompt)

    elif rag_tool == "Opisi":
        uvod = mprompts["rag_self_query"]
        combined_prompt = uvod + prompt
        context = SelfQueryDelfi(combined_prompt)

    elif rag_tool == "Korice":
        uvod = mprompts["rag_self_query"]
        combined_prompt = uvod + prompt
        context = SelfQueryDelfi(upit=combined_prompt, namespace="korice")

    elif rag_tool == "Graphp":
        context = graphp(prompt)

    elif rag_tool == "Pineg":
        context = pineg(prompt)

    elif rag_tool == "Natop":
        context = get_items_by_category(prompt)

    elif rag_tool == "Orders":
        context = order_delfi(prompt)

    return context, rag_tool


def get_structured_decision_from_model(user_query: str) -> str:
    """
    Determines the appropriate tool to handle a user's query using the OpenAI model.

    This function sends the user's query to the OpenAI API with a specific system prompt to obtain a structured
    decision in JSON format. It parses the JSON response to extract the selected tool.

    Args:
        user_query (str): The user's input query for which a structured decision is to be made.

    Returns:
        str: The name of the tool determined by the model to handle the user's query. If the 'tool' key is not present,
             it returns the first value from the JSON response.
    """
    client = OpenAI()
    response = client.chat.completions.create(
        model=getenv("OPENAI_MODEL"),
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
        {"role": "system", "content": mprompts["choose_rag"]},
        {"role": "user", "content": f"Please provide the response in JSON format: {user_query}"}],
        )
    json_string = response.choices[0].message.content
    # Parse the JSON string into a Python dictionary
    data_dict = json.loads(json_string)
    # Access the 'tool' value
    return data_dict['tool'] if 'tool' in data_dict else list(data_dict.values())[0]


def graphp(pitanje):
    """
    Processes a user's question, generates a Cypher query, executes it on a Neo4j database, 
    enriches the resulting data with descriptions from Pinecone, and formats the response.

    Parameters:
    pitanje (str): User's question in natural language related to the Neo4j database.

    Returns:
    list: A list of dictionaries representing enriched book data, each containing properties like 
          'title', 'category', 'author', and a description from Pinecone.
    
    The function consists of the following steps:
    1. Connects to the Neo4j database using the `connect_to_neo4j()` function.
    2. Defines a nested function `run_cypher_query()` to execute a Cypher query and clean the results.
    3. Generates a Cypher query from the user's question using the `generate_cypher_query()` function.
    4. Validates the generated Cypher query using `is_valid_cypher()`.
    5. Runs the Cypher query on the Neo4j database and retrieves book data.
    6. Enriches the retrieved book data with additional information fetched from an API.
    7. Fetches descriptions from Pinecone for the books using their 'oldProductId'.
    8. Combines book data with descriptions.
    9. Formats the final data and returns it as a formatted response.

    The function performs error handling to manage invalid Cypher queries or errors during data fetching.
    """
    driver = connect_to_neo4j()

    def run_cypher_query(driver, query):
        with driver.session() as session:
            results = session.run(query)
            cleaned_results = []
            max_characters=100000
            total_characters = 0
            max_record_length = 0
            min_record_length = float('inf')
            
            for record in results:
                cleaned_record = {}
                for key, value in record.items():
                    if isinstance(value, neo4j.graph.Node):
                        # Ako je vrednost Node objekat, pristupamo properties atributima
                        properties = {k: v for k, v in value._properties.items()}
                    else:
                        # Ako je vrednost obična vrednost, samo je dodamo
                        properties = {key: value}
                    
                    for prop_key, prop_value in properties.items():
                        # Uklanjamo prefiks 'b.' ako postoji
                        new_key = prop_key.split('.')[-1]
                        cleaned_record[new_key] = prop_value
                
                record_length = sum(len(str(value)) for value in cleaned_record.values())
                if total_characters + record_length > max_characters:
                    break  # Prekida se ako dodavanje ovog zapisa prelazi maksimalan broj karaktera

                cleaned_results.append(cleaned_record)
                record_length = sum(len(str(value)) for value in cleaned_record.values())
                total_characters += record_length
                if record_length > max_record_length:
                    max_record_length = record_length
                if record_length < min_record_length:
                    min_record_length = record_length
        
        number_of_records = len(cleaned_results)
        # average_characters_per_record = total_characters / number_of_records if number_of_records > 0 else 0

        print(f"Number of records: {number_of_records}")
        print(f"Total number of characters: {total_characters}")

        return cleaned_results
        

    def generate_cypher_query(question):
        prompt = f"Translate the following user question into a Cypher query. Use the given structure of the database: {question}"
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            messages=[
                {
            "role": "system",
            "content": (
                "You are a helpful assistant that converts natural language questions into Cypher queries for a Neo4j database."
                "The database has 3 node types: Author, Book, Genre, and 2 relationship types: BELONGS_TO and WROTE."
                "Only Book nodes have properties: id, oldProductId, category, title, price, quantity, pages, and eBook."
                "All node and relationship names are capitalized (e.g., Author, Book, Genre, BELONGS_TO, WROTE)."
                "Genre names are also capitalized (e.g., Drama, Fantastika, Domaći pisci, Knjige za decu). Please ensure that the generated Cypher query uses these exact capitalizations."
                "Sometimes you will need to filter the data based on the category. Exsiting categories are: Knjiga, Strana knjiga, Gift, Film, Muzika, Udžbenik, Video igra, Dečija knjiga."
                "Ensure to include a condition to check that the quantity property of Book nodes is greater than 0 to ensure the books are in stock where this filter is plausable."
                "When writing the Cypher query, ensure that instead of '=' use CONTAINS, in order to return all items which contains the searched term."
                "When generating the Cypher query, ensure to handle inflected forms properly by converting all names to their nominative form. For example, if the user asks for books by 'Adrijana Čajkovskog,' the query should be generated for 'Adrijan Čajkovski,' ensuring that the search is performed using the base form of the author's name."
                "Additionally, ensure to normalize the search term by replacing non-diacritic characters with their diacritic equivalents. For instance, convert 'z' to 'ž', 's' to 'š', 'c' to 'ć' or 'č', and so on, so that the search returns accurate results even when the user omits Serbian diacritics."
                "When returning some properties of books, ensure to always return the oldProductId and the title too."
                "Ensure to limit the number of records returned to 6."
                "Hari Poter is stored as 'Harry Potter' in the database."

                "Here is an example user question and the corresponding Cypher query: "
                "Example user question: 'Pronađi knjigu Da Vinčijev kod.' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Da Vinčijev kod') AND b.quantity > 0 RETURN b LIMIT 6"

                "Example user question: 'O čemu se radi u knjizi Memoari jedne gejše?' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Memoari jedne gejše') RETURN b LIMIT 6"

                "Example user question: 'Interesuje me knjiga Piramide.' "
                "Cypher query: MATCH (b:Book)-[:WROTE]-(a:Author) WHERE toLower(b.title) CONTAINS toLower('Piramide') AND b.quantity > 0 RETURN b.title AS title, b.oldProductId AS oldProductId, b.category AS category, a.name AS author LIMIT 6"
                
                "Example user question: 'Preporuci mi knjige istog žanra kao Krhotine.' "
                "Cypher query: MATCH (b:Book)-[:BELONGS_TO]->(g:Genre) WHERE toLower(b.title) CONTAINS toLower('Krhotine') WITH g MATCH (rec:Book)-[:BELONGS_TO]->(g)<-[:BELONGS_TO]-(b:Book) WHERE b.title CONTAINS 'Krhotine' AND rec.quantity > 0 MATCH (rec)-[:WROTE]-(a:Author) RETURN rec.title AS title, rec.oldProductId AS oldProductId, b.category AS category, a.name AS author, g.name AS genre LIMIT 6"

                "Example user question: 'Koja je cena za Autostoperski vodič kroz galaksiju?' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Autostoperski vodič kroz galaksiju') AND b.quantity > 0 RETURN b.title AS title, b.oldProductId AS oldProductId, b.category AS category LIMIT 6"

                "Example user question: 'Da li imate anu karenjinu na stanju' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Ana Karenjina') AND b.quantity > 0 RETURN b.title AS title, b.oldProductId AS oldProductId, b.category AS category LIMIT 6"

                "Example user question: 'Intresuju me fantastika. Preporuči mi neke knjige' "
                "Cypher query: MATCH (a:Author)-[:WROTE]->(b:Book)-[:BELONGS_TO]->(g:Genre {name: 'Fantastika'}) RETURN b, a.name, g.name LIMIT 6"
                
                "Example user question: 'Da li imate mobi dik na stanju, treba mi 27 komada?' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Mobi Dik') AND b.quantity > 27 RETURN b.title AS title, b.quantity AS quantity, b.oldProductId AS oldProductId, b.category AS category LIMIT 6"
            
                "Example user question: 'preporuči mi knjige slične Oladi malo od Sare Najt' "
                "Cypher query: MATCH (b:Book)-[:WROTE]-(a:Author) WHERE toLower(b.title) CONTAINS toLower('Oladi malo') AND toLower(a.name) CONTAINS toLower('Sara Najt') WITH b MATCH (b)-[:BELONGS_TO]->(g:Genre) WITH g, b MATCH (rec:Book)-[:BELONGS_TO]->(g)<-[:BELONGS_TO]-(b) WHERE rec.quantity > 0 AND NOT toLower(rec.title) CONTAINS toLower('Oladi malo') WITH rec, COLLECT(DISTINCT g.name) AS genres MATCH (rec)-[:WROTE]-(recAuthor:Author) RETURN rec.title AS title, rec.oldProductId AS oldProductId, rec.category AS category, recAuthor.name AS author, genres AS genre LIMIT 6"
            )
        },
                {"role": "user", "content": prompt}
            ]
        )
        cypher_query = response.choices[0].message.content.strip()

        # Uklanjanje nepotrebnog teksta oko upita
        if '```cypher' in cypher_query:
            cypher_query = cypher_query.split('```cypher')[1].split('```')[0].strip()
        
        # Uklanjanje tačke ako je prisutna na kraju
        if cypher_query.endswith('.'):
            cypher_query = cypher_query[:-1].strip()

        return cypher_query


    def get_descriptions_from_pinecone(ids):
        # Initialize Pinecone
        # pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"), host=os.getenv("PINECONE_HOST"))
        index = connect_to_pinecone(x=0)
        # Fetch the vectors by IDs
        try:
            results = index.fetch(ids=ids, namespace="opisi")
        except Exception as e:
            print(f"Error fetching vectors: {e}")
            return {}
        descriptions = {}
        for id in ids:
            if id in results['vectors']:
                vector_data = results['vectors'][id]
                if 'metadata' in vector_data:
                    descriptions[id] = vector_data['metadata'].get('text', 'No description available')
                else:
                    descriptions[id] = 'Metadata not found in vector data.'
            else:
                descriptions[id] = 'Nemamo opis za ovaj artikal.'
        
        return descriptions
    

    def combine_data(book_data, descriptions):
        # print(f"Book Data: {book_data}")
        # print(f"Descriptions: {descriptions}")
        combined_data = []

        for book in book_data:        
            book_id = book.get('oldProductId', None)
            
            # Konvertuj book_id u string da bi se mogao porediti sa ključevima u descriptions
            book_id_str = str(book_id)

            description = descriptions.get(book_id_str, 'No description available')
            combined_entry = {**book, 'description': description}
            combined_data.append(combined_entry)
        
        # print(f"Combined Data: {combined_data}")
        return combined_data


    def display_results(combined_data):
        x = ""
        for data in combined_data:
            # print(f"Data iz display_results: {data}")
            if 'title' in data:
                x += f"Naslov: {data['title']}\n"
            if 'category' in data:
                x += f"Kategorija: {data['category']}\n"
            if 'puna cena' in data:
                x += f"Puna cena: {data['puna cena']}\n"
            if 'author' in data:
                x += f"Autor: {data['author']}\n"
            if 'lager' in data:
                x += f"Količina: {data['lager']}\n"
            if 'pages' in data:
                x += f"Broj strana: {data['pages']}\n"
            if 'eBook' in data:
                x += f"eBook: {data['eBook']}\n"
            if 'description' in data:
                x += f"Opis: {data['description']}\n"
            if 'url' in data:
                x += f"Link: {data['url']}\n"
            if 'cena sa redovnim popustom' in data:
                x += f"Cena sa redovnim popustom: {data['cena sa redovnim popustom']}\n"
            if 'cena sa redovnim popustom na količinu' in data:
                x += f"Cena sa redovnim popustom na količinu: {data['cena sa redovnim popustom na količinu']}\n"
            if 'limit za količinski popust' in data:
                x += f"Limit za količinski popust: {data['limit za količinski popust']}\n"
            if 'cena sa premium popustom' in data:
                x += f"Cena sa premium popustom: {data['cena sa premium popustom']}\n"
            if 'cena sa premium popustom na količinu' in data:
                x += f"Cena sa premium popustom na količinu: {data['cena sa premium popustom na količinu']}\n"
            if 'limit za količinski premium popust' in data:
                x += f"Limit za količinski premium popust: {data['limit za količinski premium popust']}\n"
            if 'naziv akcije' in data:
                x += f"Naziv akcije: {data['naziv akcije']}\n"
            if 'početak akcije' in data:
                x += f"Početak akcije: {data['početak akcije']}\n"
            if 'kraj akcije' in data:
                x += f"Kraj akcije: {data['kraj akcije']}\n"
            if 'eksponencijalni procenti' in data:
                x += f"Eksponencijalni procenti: {data['eksponencijalni procenti']}\n"
            if 'eksponencijalni procenti na kolicinu' in data:
                x += f"Eksponencijalni procenti na kolicinu: {data['eksponencijalni procenti na kolicinu']}\n"
            x += "\n\n"
        return x


    def is_valid_cypher(cypher_query):
        # Provera validnosti Cypher upita (osnovna provera)
        if not cypher_query or "MATCH" not in cypher_query.upper():
            return False
        return True

    # def formulate_answer_with_llm(question, graph_data):
    #     input_text = f"Pitanje: '{question}'\nPodaci iz grafa: {graph_data}\nMolimo formulišite odgovor na osnovu ovih podataka."
    #     response = client.chat.completions.create(
    #         model="gpt-4o",
    #         temperature=0.0,
    #         messages=[
    #             {"role": "system", "content": "You are a helpful assistant that formulates answers based on given data. You have been provided with a user question and data returned from a graph database. Please formulate an answer based on these inputs."},
    #             {"role": "user", "content": input_text}
    #         ]
    #     )
    #     return response.choices[0].message.content.strip()
    
    cypher_query = generate_cypher_query(pitanje)
    print(f"Generated Cypher Query: {cypher_query}")
    
    if is_valid_cypher(cypher_query):
        try:
            book_data = run_cypher_query(driver, cypher_query)

            # print(f"Book Data: {book_data}")

            try:
                oldProductIds = [item['oldProductId'] for item in book_data]
                print(f"Old Product IDs: {oldProductIds}")
            except KeyError:
                print("Nema 'oldProductId'.")
                oldProductIds = []

            # Define the regex pattern to match both 'id' and 'b.id'
            pattern = r"'(?:b\.)?id': '([^']+)'"

            # Filtrirana lista koja će sadržati samo relevantne knjige
            filtered_book_data = []

            if not oldProductIds:
                filtered_book_data = book_data
                return filtered_book_data

            else:
                api_podaci = API_search(oldProductIds)
                # print(f"API Data: {api_podaci}")

                # Kreiranje mape id za brže pretraživanje
                products_info_map = {int(product['id']): product for product in api_podaci}

                # Iteracija kroz book_data i dodavanje relevantnih podataka
                for book in book_data:
                    old_id = book['oldProductId']
                    if old_id in products_info_map:
                        product = products_info_map[old_id]
                        # Spojite dva rečnika - podaci iz products_info_map ažuriraju book
                        book.update(products_info_map[old_id])
                        # Dodavanje knjige u filtriranu listu
                        filtered_book_data.append(book)

                    print(f"Filtered Book Data: {filtered_book_data}")

                print("******Gotov api deo!!!")

                oldProductIds_str = [str(id) for id in oldProductIds]

                descriptionsDict = get_descriptions_from_pinecone(oldProductIds_str)
                # print("******Gotov Pinecone deo!!!")
                combined_data = combine_data(filtered_book_data, descriptionsDict)
                
                display_results(combined_data)
                # return
                # print(f"Combined Data: {combined_data}")
                return combined_data
        except Exception as e:
            print(f"Greška pri izvršavanju upita: {e}. Molimo pokušajte ponovo.")
    else:
        print("Traženi pojam nije jasan. Molimo pokušajte ponovo.")

def pineg(pitanje):
    """
    Processes a user's question, performs a dense vector search in Pinecone, fetches relevant data from an API and Neo4j, 
    combines the results, and displays them in a structured format.

    Parameters:
    pitanje (str): User's question in natural language.

    Returns:
    list: A list of combined results, each containing information from the API, Pinecone, and Neo4j database.
    
    The function consists of the following steps:
    1. Connects to the Pinecone index using `connect_to_pinecone(x=0)` and to the Neo4j database using `connect_to_neo4j()`.
    2. Defines a nested function `run_cypher_query()` to execute a Cypher query on Neo4j to retrieve book data including 
       authors and genres.
    3. Uses `get_embedding()` to create embeddings for a given text and `dense_query()` to perform a similarity search in Pinecone.
    4. Searches Pinecone using `search_pinecone()` for the initial query and `search_pinecone_second_set()` for secondary searches.
    5. Combines book data retrieved from Neo4j and API data using `combine_data()`.
    6. Displays the final combined data in a user-friendly format using `display_results()`.
    
    The function performs error handling to avoid processing duplicate entries, limits the number of API calls to a maximum 
    of three, and returns a list of combined results with enriched book information.
    """
    index = connect_to_pinecone(x=0)
    driver = connect_to_neo4j()

    def run_cypher_query(id):
        query = f"MATCH (b:Book)-[:WROTE]-(a:Author), (b)-[:BELONGS_TO]-(g:Genre) WHERE b.oldProductId = {id} AND b.quantity > 0 RETURN b, a.name AS author, g.name AS genre"
        with driver.session() as session:
            result = session.run(query)
            book_data = []
            for record in result:
                book_node = record['b']
                existing_book = next((book for book in book_data if book['id'] == book_node['id']), None)
                if existing_book:
                    # Proveri da li su 'author' i 'genre' liste, ako nisu, konvertuj ih
                    if not isinstance(existing_book['author'], list):
                        existing_book['author'] = [existing_book['author']]
                    if not isinstance(existing_book['genre'], list):
                        existing_book['genre'] = [existing_book['genre']]

                    # Ako postoji, dodaj autora i žanr u postojeće liste ako nisu već tamo
                    if record['author'] not in existing_book['author']:
                        existing_book['author'].append(record['author'])
                    if record['genre'] not in existing_book['genre']:
                        existing_book['genre'].append(record['genre'])
                else:
                    # Ako ne postoji, dodaj novi zapis sa autorom i žanrom kao liste
                    book_data.append({
                        'id': book_node['id'],
                        'oldProductId': book_node['oldProductId'],
                        'title': book_node['title'],
                        'author': record['author'],
                        'category': book_node['category'],
                        'genre': record['genre'],
                        'price': book_node['price'],
                        'quantity': book_node['quantity'],
                        'pages': book_node['pages'],
                        'eBook': book_node['eBook']
                })
            # print(f"Book Data: {book_data}")
            return book_data

    def get_embedding(text, model="text-embedding-3-large"):
        response = client.embeddings.create(
            input=[text],
            model=model
        ).data[0].embedding
        # print(f"Embedding Response: {response}")
        
        return response

    def dense_query(query, top_k, filter, namespace="opisi"):
        # Get embedding for the query
        dense = get_embedding(text=query)
        # print(f"Dense: {dense}")

        query_params = {
            'top_k': top_k,
            'vector': dense,
            'include_metadata': True,
            'filter': filter,
            'namespace': namespace
        }

        response = index.query(**query_params)

        matches = response.to_dict().get('matches', [])
        # print(f"Matches: {matches}")
        matches.sort(key=lambda x: x['score'], reverse=True)

        return matches

    def search_pinecone(query: str) -> List[Dict]:
        # Dobij embedding za query
        query_embedding = dense_query(query, top_k=4, filter=None)
        # print(f"Results: {query_embedding}")

        # Ekstraktuj id i text iz metapodataka rezultata
        matches = []
        for match in query_embedding:
            metadata = match['metadata']
            matches.append({
                'id': metadata['id'],
                'sec_id': int(metadata['sec_id']),
                'text': metadata['text'],
                'authors': metadata['authors'],
                'title': metadata['title']
            })
        
        return matches

    def search_pinecone_second_set(title: str, authors: str ) -> List[Dict]:
        # Dobij embedding za query
        query = "Nađi knjigu"
        filter = {"title" : {"$eq" : title}, "authors" : {"$in" : authors}}
        query_embedding_2 = dense_query(query, top_k=5, filter=filter)
        # print(f"Results: {query_embedding}")

        # Ekstraktuj id i text iz metapodataka rezultata
        matches = []
        for match in query_embedding_2:
            metadata = match['metadata']
            matches.append({
                'id': metadata['id'],
                'sec_id': int(metadata['sec_id']),
                'text': metadata['text'],
                'authors': metadata['authors'],
                'title': metadata['title']
            })
        
        # print(f"Matches: {matches}")
        return matches

    def combine_data(api_data, book_data, description):
        combined_data = []
        for book in book_data:
            # Pronađi odgovarajući unos u api_data na osnovu oldProductId
            matching_api_entry = next((item for item in api_data if str(item['id']) == str(book['oldProductId'])), None)
            
            if matching_api_entry:
                # Uzmemo samo potrebna polja iz book_data
                selected_book_data = {
                    'title': book.get('title'),
                    'author': book.get('author', []),
                    'category': book.get('category'),
                    'genre': book.get('genre', []),
                    'pages': book.get('pages'),
                    'eBook': book.get('eBook')
                }
                combined_entry = {
                    **selected_book_data,  # Dodaj samo potrebna polja iz book_data
                    **matching_api_entry,  # Dodaj sve podatke iz api_data
                    'description': description  # Dodaj opis
                }
            
            combined_data.append(combined_entry)

        return combined_data

    def display_results(combined_data):
        x = ""
        for data in combined_data:
            print(f"Data iz display_results: {data}")
            if "title" in data:
                print(f"Naziv: {data['title']}")
                x += f"Naslov: {data['title']}\n"
            if "author" in data:
                x += f"Autor: {data['author']}\n"
            if "category" in data:
                x += f"Kategorija: {data['category']}\n"
            if "genre" in data:
                x += f"Žanr: {(data['genre'])}\n"
            if "puna cena" in data:
                x += f"Cena: {data['puna cena']}\n"
            if "lager" in data:
                x += f"Dostupnost: {data['lager']}\n"
            if "pages" in data:
                x += f"Broj stranica: {data['pages']}\n"
            if "eBook" in data:
                x += f"eBook: {data['eBook']}\n"
            if "description" in data:
                x += f"Opis: {data['description']}\n"
            if "url" in data:
                x += f"Link: {data['url']}\n"
            if 'cena sa redovnim popustom' in data:
                x += f"Cena sa redovnim popustom: {data['cena sa redovnim popustom']}\n"
            if 'cena sa redovnim popustom na količinu' in data:
                x += f"Cena sa redovnim popustom na količinu: {data['cena sa redovnim popustom na količinu']}\n"
            if 'limit za količinski popust' in data:
                x += f"Limit za količinski popust: {data['limit za količinski popust']}\n"
            if 'cena sa premium popustom' in data:
                x += f"Cena sa premium popustom: {data['cena sa premium popustom']}\n"
            if 'cena sa premium popustom na količinu' in data:
                x += f"Cena sa premium popustom na količinu: {data['cena sa premium popustom na količinu']}\n"
            if 'limit za količinski premium popust' in data:
                x += f"Limit za količinski premium popust: {data['limit za količinski premium popust']}\n"
            x += "\n\n"

        return x

    search_results = search_pinecone(pitanje)
    print(f"Search Results: {search_results}")

    combined_results = []
    duplicate_filter = []
    counter = 0

    for result in search_results:
        print(f"Result: {result}")
        if result['sec_id'] in duplicate_filter:
            print(f"Duplicate Filter: {duplicate_filter}")
            continue
        else:
            if counter < 3:
                api_data = API_search([result['sec_id']])
                # print(f"API Data: {api_data}")
                if api_data:
                    counter += 1
                    print(f"Counter: {counter}")
                else:
                    print(f"API Data is empty for sec_id: {result['sec_id']}")
                    title = result['title']
                    authors = result['authors']
                    search_results_2 = search_pinecone_second_set(title, authors)
                    for result_2 in search_results_2:
                        if result_2['sec_id'] in duplicate_filter:
                            continue
                        else:
                            api_data = API_search([result_2['sec_id']])
                            # print(f"API Data 2: {api_data}")
                            if api_data:
                                counter += 1
                                # print(f"Counter 2: {counter}")
                                data = run_cypher_query(result_2['sec_id'])
                                # print(f"Data: {data}")

                                combined_data = combine_data(api_data, data, result_2['text'])
                                # print(f"Combined Data: {combined_data}")
                                duplicate_filter.append(result_2['sec_id'])
                                
                                combined_results.append(combined_data)
                            
                                # display_results(combined_data)
                                break

                    continue # Preskoči ako je api_data prazan

                data = run_cypher_query(result['sec_id'])
                # print(f"Data: {data}")

                combined_data = combine_data(api_data, data, result['text'])
                # print(f"Combined Data: {combined_data}")
                duplicate_filter.append(result['sec_id'])
                # print(f"Duplicate Filter: {duplicate_filter}")
                
                combined_results.extend(combined_data)
                # print(f"Combined Results: {combined_results}")
                
                
                # return display_results(combined_data)
            else:
                break
    display_results(combined_data)
    # print(f"Combined Results: {combined_results}")
    # print(f"Display Results: {display_results(combined_results)}")
    return combined_results


def get_items_by_category(prompt: str) -> str:
    """
    Retrieves items from a specific category based on the user's prompt.

    This function uses the OpenAI API to determine the category of the user's query. It then sends a GET request
    to an external API to fetch items belonging to the identified category. The function formats and returns
    the relevant item details such as title, authors, and genres.

    Args:
        prompt (str): The user's input prompt used to determine the category of items to retrieve.

    Returns:
        str: A formatted string containing the details of items in the identified category. If an error occurs
             during the API request, it returns an error message.
    """
    response = client.chat.completions.create(
        model=getenv("OPENAI_MODEL"),
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
        {"role": "system", "content": """You are a helpful assistant that determines the category of the user's query. It must be one of the following 3: 
         Knjiga, Strana knjiga, Gift
         
         You may only return the name of the category (with the capitalization as provided above). Do not include any additional information."""},
        {"role": "user", "content": f"Please provide the response in JSON format: {prompt}"}],
        )
    data_dict = json.loads(response.choices[0].message.content)
    # Access the 'tool' value
    category = data_dict['tool'] if 'tool' in data_dict else list(data_dict.values())[0]
    
    try:
        # Slanje GET zahteva prema API-ju
        response = requests.get("https://delfi.rs/api/pc-frontend-api/toplists")
        response.raise_for_status()  # Provera uspešnosti zahteva

        # Parsiranje odgovora iz JSON formata
        result_string = ""
        data = response.json()
        for item in data.get('data', {}).get('sections', []):
            for product in item.get('content', {}).get('products', []):
                if product.get('category') == category:
                    title = product.get('title', 'N/A')
                    authors = product.get('authors', [])
                    genres = product.get('genres', [])
                    
                    # Convert authors and genres to a string format
                    authors_str = ', '.join([author.get('authorName', 'Unknown') for author in authors])
                    genres_str = ', '.join([genre.get('genreName', 'Unknown') for genre in genres])
                    
                    # Append the collected information to the result string
                    result_string += f"Title: {title}\n"
                    result_string += f"Authors: {authors_str}\n"
                    result_string += f"Genres: {genres_str}\n"
                    result_string += "-" * 40 + "\n"  # Separator for readability
                    
        return result_string

    except requests.exceptions.RequestException as e:
        return f"Došlo je do greške prilikom povezivanja sa API-jem: {e}"


def API_search_2(order_ids: List[str]) -> Union[List[Dict[str, Any]], str]:
    """
    Retrieves and processes information for a list of order IDs.

    This function fetches detailed information for each order ID by making API requests to an external service.
    It parses the JSON responses to extract relevant order details such as ID, type, status, delivery service,
    delivery time, payment type, package status, and order item type. Additionally, it collects tracking codes
    and performs an auxiliary search if tracking codes are available.

    Args:
        order_ids (List[str]): A list of order IDs for which information is to be retrieved.

    Returns:
        List[Dict[str, Any]] or str: A list of dictionaries containing the extracted order information. If an error
                                     occurs during the retrieval process, it returns an error message indicating that
                                     no orders were found for the given IDs.
    """
    def get_order_info(order_id):
        url = f"http://185.22.145.64:3003/api/order-info/{order_id}"
        headers = {
            'x-api-key': getenv("DELFI_ORDER_API_KEY")
        }
        return requests.get(url, headers=headers).json()
    tc = []
    # Function to parse the JSON response and extract required fields
    def parse_order_info(json_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses the JSON data for a single order and extracts relevant order information.

        Args:
            json_data (Dict[str, Any]): The JSON data received from the order information API.

        Returns:
            Dict[str, Any]: A dictionary containing extracted order details such as:
                - id (str): The unique identifier of the order.
                - type (str): The type of the order.
                - status (str): The current status of the order.
                - delivery_service (str): The delivery service used for the order.
                - delivery_time (str): The estimated delivery time.
                - payment_type (str): The type of payment used.
                - package_status (str): The status of the package.
                - order_item_type (str): The type of items in the order.
        """
        order_info = {}
        if 'orderData' in json_data:
            data = json_data['orderData']
            # Extract required fields from the order info
            order_info['id'] = data.get('id', 'N/A')
            order_info['type'] = data.get('type', 'N/A')
            order_info['status'] = data.get('status', 'N/A')
            order_info['delivery_service'] = data.get('delivery_service', 'N/A')
            order_info['delivery_time'] = data.get('delivery_time', 'N/A')
            order_info['payment_type'] = data.get('payment_detail', {}).get('payment_type', 'N/A')
            tc.append(data.get('tracking_codes', None))
            # Extract package info if available
            packages = data.get('packages', [])
            if packages:
                package_status = packages[0].get('status', 'N/A')
                order_info['package_status'] = package_status

            # Extract order items info if available
            order_items = data.get('order_items', [])
            if order_items:
                item_type = order_items[0].get('type', 'N/A')
                order_info['order_item_type'] = item_type

        return order_info

    # Main function to get info for a list of order IDs
    def get_multiple_orders_info(order_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Retrieves and processes information for multiple order IDs.

        Args:
            order_ids (List[str]): A list of order IDs for which information is to be retrieved.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries, each containing details of an order, including:
                - id (str): The unique identifier of the order.
                - type (str): The type of the order.
                - status (str): The current status of the order.
                - delivery_service (str): The delivery service used for the order.
                - delivery_time (str): The estimated delivery time.
                - payment_type (str): The type of payment used.
                - package_status (str): The status of the package.
                - order_item_type (str): The type of items in the order.
            If an error occurs during retrieval, the list may contain an error message string.
        """
        orders_info = []
        for order_id in order_ids:
            json_data = get_order_info(order_id)
            print(json_data)  # Debugging print to see raw JSON response
            order_info = parse_order_info(json_data)
            if order_info:
                orders_info.append(order_info)
        return orders_info

    # Retrieve order information for all provided order IDs
    try:
        orders_info = get_multiple_orders_info(order_ids)
    except Exception as e:
        print(f"Error retrieving order information: {e}")
        orders_info = "No orders found for the given IDs."
    tc = [x for x in tc if x is not None]
    if len(tc) > 0:
        orders_info.append(API_search_aks(tc))

    return orders_info


import re
def order_delfi(prompt: str) -> str:
    def extract_orders_from_string(text: str) -> List[int]:
        """
        Extracts all integer order IDs consisting of five or more digits from the provided text.

        Args:
            text (str): The input string containing potential order IDs.

        Returns:
            List[int]: A list of extracted order IDs as integers.
        """
        # Define a regular expression pattern to match 5 or more digit integers
        pattern = r'\b\d{5,}\b'
        
        # Use re.findall to extract all matching patterns
        orders = re.findall(pattern, text)
        
        # Convert the matched strings to integers
        return [int(order) for order in orders]

    order_ids = extract_orders_from_string(prompt)
    print(order_ids)
    if len(order_ids) > 0:
        return API_search_2(order_ids)
        if o[0]['package_status'] == "MAIL_SENT":
            return "Nema informacija o porudžbini."
    else:
        return "Morate uneti tačan broj porudžbine/a."


def API_search(matching_sec_ids: List[int]) -> List[Dict[str, Any]]:

    def get_product_info(token, product_id):
        return requests.get(url="https://www.delfi.rs/api/products", params={"token": token, "product_id": product_id}).content

    # Function to parse the XML response and extract required fields
    def parse_product_info(xml_data: bytes) -> Dict[str, Any]:
        """
        Parses the XML data of a product and extracts relevant product information.

        Args:
            xml_data (bytes): The XML data retrieved from the Delfi API for a product.

        Returns:
            Dict[str, Any]: A dictionary containing extracted product details such as prices, lager, URL, ID, and action information.
                            The dictionary may include keys like:
                                - 'puna cena' (float)
                                - 'eBook cena' (float)
                                - 'lager' (str)
                                - 'url' (str)
                                - 'id' (str)
                                - 'akcija' (Dict[str, Any], optional)
                                - 'cene' (Dict[str, Any], optional)
        """
        product_info = {}
        try:
            root = ET.fromstring(xml_data)
            product_node = root.find(".//product")
            if product_node is not None:
                # cena = product_node.findtext('cena')
                lager = product_node.findtext('lager')
                url = product_node.findtext('url')
                id = product_node.findtext('ID')

                action_node = product_node.find('action')
                if action_node is not None:
                    print(f"Action node found!")  # Debugging line
                    type = action_node.find('type').text
                    if type == "fixedPrice" or type == "fixedDiscount":
                        title = action_node.find('title').text
                        start_at = action_node.find('startAt').text
                        end_at = action_node.find('endAt').text
                        price_regular_standard = float(action_node.find('priceRegularStandard').text)
                        price_regular_premium = float(action_node.find('priceRegularPremium').text)
                        price_quantity_standard = float(action_node.find('priceQuantityStandard').text)
                        price_quantity_premium = float(action_node.find('priceQuantityPremium').text)

                        akcija = {
                            'naziv akcije': title,
                            'početak akcije': start_at,
                            'kraj akcije': end_at,
                            'cena sa redovnim popustom': price_regular_standard,
                            'cena sa premium popustom': price_regular_premium,
                            'cena sa redovnim količinskim popustom': price_quantity_standard,
                            'cena sa premium količinskim popustom': price_quantity_premium
                        }
                    elif type == "exponentialDiscount":
                        title = action_node.find('title').text
                        start_at = action_node.find('startAt').text
                        end_at = action_node.find('endAt').text
                        eksponencijalni_procenti = action_node.find('levelPercentages')
                        eksponencijalne_cene = action_node.find('levelPrices')

                        akcija = {
                            'naziv akcije': title,
                            'početak akcije': start_at,
                            'kraj akcije': end_at,
                            'eksponencijalni procenti': eksponencijalni_procenti,
                            'eksponencijalne cene': eksponencijalne_cene
                        }
                    elif type == "quantityDiscount2":
                        title = action_node.find('title').text
                        start_at = action_node.find('startAt').text
                        end_at = action_node.find('endAt').text
                        price_quantity_standard_d2 = float(action_node.find('priceQuantityStandard').text)
                        price_quantity_premium_d2 = float(action_node.find('priceQuantityPremium').text)
                        quantity_discount_limit = int(action_node.find('quantityDiscount2Limit').text)

                        akcija = {
                            'naziv akcije': title,
                            'početak akcije': start_at,
                            'kraj akcije': end_at,
                            'cena sa redovnim količinskim popustom': price_quantity_standard_d2,
                            'cena sa premium količinskim popustom': price_quantity_premium_d2,
                            'limit za količinski popust': quantity_discount_limit
                        }
                else:
                    print("Action node not found, taking regular price")  # Debugging line
                    # Pristupanje priceList elementu
                price_list = product_node.find('priceList')
                if price_list is not None:
                    collection_price = float(price_list.find('collectionFullPrice').text)
                    full_price = float(price_list.find('fullPrice').text)
                    eBook_price = float(price_list.find('eBookPrice').text)
                    regular_discount_price = float(price_list.find('regularDiscountPrice').text)
                    regular_discount_percentage = float(price_list.find('regularDiscountPercentage').text)
                    quantity_discount_price = float(price_list.find('quantityDiscountPrice').text)
                    quantity_discount_percentage = float(price_list.find('quantityDiscountPercentage').text)
                    quantity_discount_limit = int(price_list.find('quantityDiscountLimit').text)
                    premium_discount_price = float(price_list.find('regularDiscountPremiumPrice').text)
                    premium_discount_percentage = float(price_list.find('regularDiscountPremiumPercentage').text)
                    premium_quantity_discount_price = float(price_list.find('quantityDiscountPremiumPrice').text)
                    premium_quantity_discount_percentage = float(price_list.find('quantityDiscountPremiumPercentage').text)
                    premium_quantity_discount_limit = int(price_list.find('quantityDiscountPremiumLimit').text)

                    cene = {
                        'cena kolekcije': collection_price,
                        'cena sa redovnim popustom': regular_discount_price,
                        'cena sa redovnim popustom na količinu': quantity_discount_price,
                        'limit za količinski popust': quantity_discount_limit,
                        'cena sa premium popustom': premium_discount_price,
                        'cena sa premium popustom na količinu': premium_quantity_discount_price,
                        'limit za količinski premium popust': premium_quantity_discount_limit
                    }
                
                # if lager and int(lager) > 0:
                if int(lager) > 0:
                    product_info = {
                        'puna cena': full_price,
                        'eBook cena': eBook_price,
                        'lager': lager,
                        'url': url,
                        'id': id
                    }
                    if action_node is None:
                        product_info.update(cene)
                    else:
                        product_info.update(akcija)
                else:
                    print(f"Skipping product with lager {lager}")  # Debugging line
            else:
                print("Product node not found in XML data")  # Debugging line
        except ET.ParseError as e:
            print(f"Error parsing XML: {e}")  # Debugging line
        return product_info

    # Main function to get info for a list of product IDs
    def get_multiple_products_info(token: str, product_ids: List[int]) -> Union[List[Dict[str, Any]], str]:
        """
        Retrieves and processes information for multiple product IDs.

        Args:
            token (str): The API authentication token.
            product_ids (List[int]): A list of product IDs for which information is to be retrieved.

        Returns:
            Union[List[Dict[str, Any]], str]: 
                - If successful, returns a list of dictionaries, each containing details of a product.
                - If an error occurs during retrieval, returns an error message string indicating that no products were found for the given IDs.
        """
        products_info = []
        for product_id in product_ids:
            # print(f"Product ID: {product_id}")
            xml_data = get_product_info(token, product_id)
            # print(f"XML data for product_id {product_id}: {xml_data}")  # Debugging line
            product_info = parse_product_info(xml_data)
            if product_info:
                products_info.append(product_info)
        return products_info

    # Replace with your actual token and product IDs
    token = os.getenv("DELFI_API_KEY")
    product_ids = matching_sec_ids

    try:
        products_info = get_multiple_products_info(token, product_ids)
    except:
        products_info = "No products found for the given IDs."
    # print(f"API Info: {products_info}")
    # output = "Data returned from API for each searched id: \n"
    # for info in products_info:
    #     output += str(info) + "\n"
    return products_info


def API_search_aks(order_ids: List[str]) -> List[Dict[str, Any]]:
    
    def get_order_status(order_id: int) -> Dict[str, Any]:
        url = f"http://www.akskurir.com/AKSVipService/Pracenje/{order_id}"
        response = requests.get(url)
        response.raise_for_status()  # Raise an error for failed requests
        return response.json()

    def parse_order_status(json_data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Parses the JSON data of an order's status and extracts relevant information.

        Args:
            json_data (Dict[str, Any]): The JSON data received from the order tracking API.

        Returns:
            Tuple[Dict[str, Any], List[Dict[str, Any]]]: 
                - A dictionary containing the error code and current status of the order.
                - A list of dictionaries detailing each status change, including:
                    - 'Vreme' (str): The timestamp of the status change.
                    - 'VremeInt' (str): An internal timestamp or identifier.
                    - 'Centar' (str): The center or location associated with the status.
                    - 'StatusOpis' (str): A description of the status.
                    - 'NStatus' (str): A numerical or coded representation of the status.
        """
        status_info = {}
        status_changes = []
        
        if 'ErrorCode' in json_data and json_data['ErrorCode'] == 0:
            status_info['ErrorCode'] = json_data.get('ErrorCode', 'N/A')
            status_info['Status'] = json_data.get('Status', 'N/A')
            
            lst = json_data.get('StatusList', [])
            for status in lst:
                status_change = {
                    'Vreme': status.get('Vreme', 'N/A'),
                    'VremeInt': status.get('VremeInt', 'N/A'),
                    'Centar': status.get('Centar', 'N/A'),
                    'StatusOpis': status.get('StatusOpis', 'N/A'),
                    'NStatus': status.get('NStatus', 'N/A')
                }
                status_changes.append(status_change)
        else:
            status_info['ErrorCode'] = json_data.get('ErrorCode', 'N/A')
            status_info['Status'] = json_data.get('Status', 'N/A')

        return status_info, status_changes

    def get_multiple_orders_info(order_ids: List[int]) -> List[Dict[str, Any]]:
        """
        Retrieves and processes information for multiple order IDs.

        Args:
            order_ids (List[int]): A list of order IDs for which information is to be retrieved.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries, each containing:
                - 'order_id' (int): The unique identifier of the order.
                - 'current_status' (Dict[str, Any]): The current status information of the order, including:
                    - 'ErrorCode' (Any): The error code returned by the API (if any).
                    - 'Status' (Any): The current status of the order.
                - 'status_changes' (List[Dict[str, Any]]): A list of status change records, each containing:
                    - 'Vreme' (str): The timestamp of the status change.
                    - 'VremeInt' (str): An internal timestamp or identifier.
                    - 'Centar' (str): The center or location associated with the status.
                    - 'StatusOpis' (str): A description of the status.
                    - 'NStatus' (str): A numerical or coded representation of the status.
                - 'error' (str, optional): An error message if the order information could not be retrieved.
        """
        orders_info = []
        for order_id in order_ids:
            try:
                # Fetch order status
                order_status_json = get_order_status(order_id)
                current_status, status_changes = parse_order_status(order_status_json)
                
                # Assemble order information
                order_info = {
                    'order_id': order_id,
                    'current_status': current_status,
                    'status_changes': status_changes
                }
                orders_info.append(order_info)
            except requests.exceptions.RequestException as e:
                print(f"HTTP error for order {order_id}: {e}")
                orders_info.append({'order_id': order_id, 'error': str(e)})
            except Exception as e:
                print(f"Error for order {order_id}: {e}")
                orders_info.append({'order_id': order_id, 'error': str(e)})
        return orders_info

    # Main function to retrieve information for all orders
    try:
        orders_info = get_multiple_orders_info(order_ids)
    except Exception as e:
        print(f"Error retrieving order information: {e}")
        orders_info = "No orders found for the given IDs."

    return orders_info


def SelfQueryDelfi(
    upit: str,
    api_key: Optional[str] = None,
    environment: Optional[str] = None,
    index_name: str = 'delfi',
    namespace: str = 'opisi',
    openai_api_key: Optional[str] = None,
    host: Optional[str] = None
    ) -> str:
    """
    Performs a self-query on the Delfi vector store to retrieve relevant documents based on the user's query.

    This function initializes the necessary embeddings and vector store, sets up the retriever with OpenAI's ChatGPT model,
    and retrieves relevant documents that match the user's input query. It then formats the retrieved documents and their
    metadata into a single result string.

    Args:
        upit (str): The user's input query for which relevant documents are to be retrieved.
        api_key (Optional[str], optional): The API key for Pinecone. Defaults to the 'PINECONE_API_KEY' environment variable.
        environment (Optional[str], optional): The environment setting for Pinecone. Defaults to the 'PINECONE_API_KEY' environment variable.
        index_name (str, optional): The name of the Pinecone index to use. Defaults to 'delfi'.
        namespace (str, optional): The namespace within the Pinecone index to query. Defaults to 'opisi'.
        openai_api_key (Optional[str], optional): The API key for OpenAI. Defaults to the 'OPENAI_API_KEY' environment variable.
        host (Optional[str], optional): The host URL for Pinecone. Defaults to the 'PINECONE_HOST' environment variable.

    Returns:
        str: A formatted string containing the details of the retrieved documents, including metadata such as
             section ID, category, custom ID, date, image URL, authors, title, cover description, and the content.
             If an error occurs, returns the error message as a string.
    """
    
    # Use the passed values if available, otherwise default to environment variables
    api_key = api_key if api_key is not None else getenv('PINECONE_API_KEY')
    environment = environment if environment is not None else getenv('PINECONE_API_KEY')
    # index_name is already defaulted to 'positive'
    namespace = namespace if namespace is not None else getenv("NAMESPACE")
    openai_api_key = openai_api_key if openai_api_key is not None else getenv("OPENAI_API_KEY")
    host = host if host is not None else getenv("PINECONE_HOST")
   
    embeddings = OpenAIEmbeddings(model="text-embedding-3-large")

    # prilagoditi stvanim potrebama metadata
    metadata_field_info = [
        AttributeInfo(name="authors", description="The author(s) of the document", type="string"),
        AttributeInfo(name="category", description="The category of the document", type="string"),
        AttributeInfo(name="chunk", description="The chunk number of the document", type="integer"),
        AttributeInfo(name="date", description="The date of the document", type="string"),
        AttributeInfo(name="eBook", description="Whether the document is an eBook", type="boolean"),
        AttributeInfo(name="genres", description="The genres of the document", type="string"),
        AttributeInfo(name="id", description="The unique ID of the document", type="string"),
        AttributeInfo(name="text", description="The main content of the document", type="string"),
        AttributeInfo(name="title", description="The title of the document", type="string"),
        AttributeInfo(name="sec_id", description="The ID for the url generation", type="string"),
    ]

    # Define document content description
    document_content_description = "Content of the document"

    # Prilagoditi stvanom nazivu namespace-a
    text_key = "text" if namespace == "opisi" else "description"
    vectorstore = LangPine.from_existing_index(
        index_name=index_name, embedding=embeddings, text_key=text_key, namespace=namespace)

    # Initialize OpenAI embeddings and LLM
    llm = ChatOpenAI(model="gpt-4o", temperature=0.0)
    retriever = SelfQueryRetriever.from_llm(
        llm,
        vectorstore,
        document_content_description,
        metadata_field_info,
        enable_limit=True,
        verbose=True,
    )
    try:
        result = ""
        doc_result = retriever.get_relevant_documents(upit)
        for doc in doc_result:
            print("DOC: ", doc)
            metadata = doc.metadata
            print("METADATA: ", metadata)
            result += (
                (f"Sec_id: {str(metadata['sec_id'])}\n" if 'sec_id' in metadata else "") +
                (f"Category: {str(metadata['category'])}\n" if 'category' in metadata else "") +
                (f"Custom ID: {str(metadata['custom_id'])}\n" if 'custom_id' in metadata else "") +
                (f"Date: {str(int(metadata['date']))}\n" if 'date' in metadata else "") +
                (f"Image URL: {str(metadata['slika'])}\n" if 'slika' in metadata else "") +
                (f"Authors: {str(metadata.get('book_author', 'Unknown'))}\n" if 'book_author' in metadata else "") +
                (f"Title: {str(metadata.get('book_name', 'Untitled'))}\n" if 'book_name' in metadata else "") +
                (f"Cover Description: {str(metadata.get('book_cover_description', 'No description'))}\n" if 'book_cover_description' in metadata else "") +
                (f"Content: {str(doc.page_content)}\n\n" if doc.page_content else "")
            )
            print("RESULT", result)
        return result.strip()

    except Exception as e:
        print(e)
        return str(e)


class HybridQueryProcessor:
    """
    A processor for executing hybrid queries using Pinecone.

    This class allows the execution of queries that combine dense and sparse vector searches,
    typically used for retrieving and ranking information based on text data.
    """

    def __init__(self, **kwargs: Any) -> None:
        """
        Initializes the HybridQueryProcessor with optional parameters.

        The API key and environment settings are fetched from the environment variables.
        Optional parameters can be passed to override these settings.

        Args:
            **kwargs: Optional keyword arguments:
                - api_key (str): The API key for Pinecone (default fetched from environment variable).
                - environment (str): The Pinecone environment setting (default fetched from environment variable).
                - alpha (float): Weight for balancing dense and sparse scores (default 0.5).
                - score (float): Score threshold for filtering results (default 0.05).
                - index_name (str): Name of the Pinecone index to be used (default 'neo-positive').
                - namespace (str): The namespace to be used for the Pinecone index (default fetched from environment variable).
                - top_k (int): The number of results to be returned (default 5).
                - delfi_special (Any): Additional parameter for special configurations.
        """
        self.api_key = kwargs.get('api_key', getenv('PINECONE_API_KEY'))
        self.environment = kwargs.get('environment', getenv('PINECONE_API_KEY'))
        self.alpha = kwargs.get('alpha', 0.5)  # Default alpha is 0.5
        self.score = kwargs.get('score', 0.05)  # Default score is 0.05
        self.index_name = kwargs.get('index', 'neo-positive')  # Default index is 'positive'
        self.namespace = kwargs.get('namespace', getenv("NAMESPACE"))  
        self.top_k = kwargs.get('top_k', 5)  # Default top_k is 5
        self.delfi_special = kwargs.get('delfi_special')
        self.index = connect_to_pinecone(self.delfi_special)
        self.host = getenv("PINECONE_HOST")

    def get_embedding(self, text: str, model: str = "text-embedding-3-large") -> List[float]:
        """
        Retrieves the embedding for the given text using the specified model.

        Args:
            text (str): The text to be embedded.
            model (str): The model to be used for embedding. Default is "text-embedding-3-large".

        Returns:
            List[float]: The embedding vector of the given text.
        """
        
        text = text.replace("\n", " ")
        result = client.embeddings.create(input=[text], model=model).data[0].embedding
       
        return result
    
    def hybrid_score_norm(self, dense: List[float], sparse: Dict[str, Any]) -> Tuple[List[float], Dict[str, List[float]]]:
        """
        Normalizes the scores from dense and sparse vectors using the alpha value.

        Args:
            dense (List[float]): The dense vector scores.
            sparse (Dict[str, Any]): The sparse vector scores.

        Returns:
            Tuple[List[float], Dict[str, List[float]]]: 
                - Normalized dense vector scores.
                - Normalized sparse vector scores with updated values.
        """
        return ([v * self.alpha for v in dense], 
                {"indices": sparse["indices"], 
                 "values": [v * (1 - self.alpha) for v in sparse["values"]]})
    
    def hybrid_query(
        self,
        upit: str,
        top_k: Optional[int] = None,
        filter: Optional[Dict[str, Any]] = None,
        namespace: Optional[str] = None
        ) -> List[Dict[str, Any]]:
        """
        Executes a hybrid query combining both dense (embedding-based) and sparse (BM25-based) search approaches
        to retrieve the most relevant results. The query leverages embeddings for semantic understanding and
        BM25 for keyword matching, normalizing their scores for a hybrid result.

        Args:
            upit (str): The input query string for which to search and retrieve results.
            top_k (Optional[int], optional): The maximum number of top results to return. If not specified, uses the default value defined in `self.top_k`.
            filter (Optional[Dict[str, Any]], optional): An optional filter to apply to the search results. It should be a dictionary that defines criteria for filtering the results.
            namespace (Optional[str], optional): The namespace within which to search for results. Defaults to `self.namespace` if not provided.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries where each dictionary represents a search result. Each result includes metadata such as:
                - 'context': The relevant text snippet related to the query.
                - 'chunk': The specific chunk of the document where the match was found.
                - 'source': The source of the document or data (could be `None` based on certain conditions).
                - 'url': The URL of the document if available.
                - 'page': The page number if applicable.
                - 'score': The relevance score of the match (default is 0 if not present).

        Raises:
            Exception: If any error occurs during processing, the exception is caught and logged but not re-raised.

        Note:
            - The hybrid query combines both semantic and lexical retrieval methods.
            - Results are only added if the 'context' field exists in the result metadata.
            - When running under the environment variable `APP_ID="ECDBot"`, the 'source' field is conditionally modified for non-first results.
        """
        # Get embedding and unpack results
        dense = self.get_embedding(text=upit)

        # Use those results in another function call
        hdense, hsparse = self.hybrid_score_norm(
            sparse=BM25Encoder().fit([upit]).encode_queries(upit),
            dense=dense
        )

        query_params = {
            'top_k': top_k or self.top_k,
            'vector': hdense,
            'sparse_vector': hsparse,
            'include_metadata': True,
            'namespace': namespace or self.namespace
        }
        if filter:
            query_params['filter'] = filter

        response = self.index.query(**query_params)
        matches = response.to_dict().get('matches', [])
        results = []
        
        for idx, match in enumerate(matches):
            try:
                metadata = match.get('metadata', {})

                # Create the result entry with all metadata fields
                result_entry = metadata.copy()

                # Ensure mandatory fields exist with default values if they are not in metadata
                result_entry.setdefault('context', None)
                result_entry.setdefault('chunk', None)
                result_entry.setdefault('source', None)
                result_entry.setdefault('url', None)
                result_entry.setdefault('page', None)
                result_entry.setdefault('score', match.get('score', 0))

                if idx != 0 and getenv("APP_ID") == "ECDBot":
                    result_entry['source'] = None  # or omit this line to exclude 'source' entirely

                # Only add to results if 'context' exists
                if result_entry['context']:
                    results.append(result_entry)
            except Exception as e:
                # Log or handle the exception if needed
                print(f"An error occurred: {e}")
                pass
        
        return results
       
    def process_query_results(
        self,
        upit: str,
        dict: bool = False,
        device: Optional[Any] = None
        ) -> Any:
        """
        Processes the query results and prompt tokens based on relevance score and formats them for a chat or dialogue system.
        Additionally, returns a list of scores for items that meet the score threshold.

        Args:
            upit (str): The input query string to process.
            dict (bool, optional): Determines the format of the returned results. If `True`, returns a list of dictionaries containing raw results.
                                   If `False`, returns a formatted string of relevant metadata. Defaults to `False`.
            device (Optional[Any], optional): An optional device parameter to filter results, applicable when `APP_ID` is "DentyBot". Defaults to `None`.

        Returns:
            Any: 
                - If `dict` is `False`, returns a formatted string containing metadata of relevant documents.
                - If `dict` is `True`, returns a list of dictionaries with raw search results.
        """
        if getenv("APP_ID") == "DentyBot":
            filter = {'device': {'$in': [device]}}
            tematika = self.hybrid_query(upit=upit, filter=filter)
        else:
            tematika = self.hybrid_query(upit=upit)
        if not dict:
            uk_teme = ""
            
            for item in tematika:
                if item["score"] > self.score:
                    # Build the metadata string from all relevant fields
                    metadata_str = "\n".join(f"{key}: {value}" for key, value in item.items() if value != None)
                    # Append the formatted metadata string to uk_teme
                    uk_teme += metadata_str + "\n\n"
            
            return uk_teme
        else:
            return tematika


def intelisale(query: str) -> str:
    """
    Processes a user query to retrieve and generate a comprehensive customer report.

    This function connects to the 'IntelisaleTest' SQL Server database using credentials from environment variables.
    It sends the user's query to the OpenAI API to extract the client name in the standardized format 'Customer x'.
    Using the extracted client name, it executes a predefined SQL query to fetch relevant customer information,
    including details such as Code, Name, CustomerId, Branch, BlueCoatsNo, PlanCurrentYear, TurnoverCurrentYear,
    FullfilmentCurrentYear, PlaniraniIznosPoPoseti, CalculatedNumberOfVisits, PaymentAvgDays, BalanceOutOfLimit,
    BalanceCritical, and the latest activity log note.

    After retrieving the data, the function formats the results into a structured string and invokes the
    inner `generate_defined_report` function to create a formatted report in Serbian based on the fetched data.

    Args:
        query (str): The user's input query containing information to identify and retrieve the customer's details.

    Returns:
        str: A formatted report containing detailed customer information as generated by the OpenAI API.
             If an error occurs during processing, the function returns the error message as a string.
    """
    # Povezivanje na bazu podataka
    server = os.getenv('MSSQL_HOST')
    database = 'IntelisaleTest'
    username = os.getenv('MSSQL_USER')
    password = os.getenv('MSSQL_PASS')

    connection_string = (
        f'DRIVER={{ODBC Driver 18 for SQL Server}};'
        f'SERVER={server};'
        f'DATABASE={database};'
        f'UID={username};'
        f'PWD={password};'
        'Encrypt=yes;'
        'TrustServerCertificate=yes;'
    )
    
    conn = pyodbc.connect(connection_string)
    cursor = conn.cursor()

    # Unos korisnika
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.0,
        messages=[
            {
                "role": "system",
                "content": """Your only task is to return the client name from the user query.
                Client name that you return should only be in the form: 'Customer x', where x is the integer that will appear in the user query.
                So the user might call it 'Customer 15' right away, or maybe 'Company 133', or 'klijent 44', or maybe even just a number like '123', but you always return in the same format: 'Customer x'."""
            },
            {
                "role": "user",
                "content": query
            }
        ])
    
    client_name = response.choices[0].message.content.strip()

    query = """
    SELECT 
        c.Code, 
        c.Name as cn,
        c.CustomerId, 
        c.Branch, 
        c.BlueCoatsNo, 
        c.PlanCurrentYear, 
        c.TurnoverCurrentYear, 
        c.FullfilmentCurrentYear, 
        CASE 
            WHEN c.CalculatedNumberOfVisits = 0 OR c.CalculatedNumberOfVisits IS NULL THEN 0
            ELSE c.Plan12Months / 12 / NULLIF(c.CalculatedNumberOfVisits, 0)
        END AS [PlaniraniIznosPoPoseti],
        c.CalculatedNumberOfVisits,
        c.PaymentAvgDays, 
        c.BalanceOutOfLimit, 
        c.BalanceCritical,
        ac.ActivityLogNoteContent AS [PoslednjaBeleska]
    FROM 
        customers c
    LEFT JOIN 
        (
            SELECT 
                ac.CustomerID, 
                ac.ActivityLogNoteContent
            FROM 
                activities ac
            WHERE 
                ac.VisitStartDayTypeDescription = 'Poseta'
            AND 
                ac.VisitArrivalTime = (
                    SELECT MAX(VisitArrivalTime)
                    FROM activities
                    WHERE CustomerID = ac.CustomerID
                    AND VisitStartDayTypeDescription = 'Poseta'
                )
        ) ac ON c.CustomerId = ac.CustomerID
    WHERE 
        c.Name = ?
    """

    cursor.execute(query, client_name)
    rows = cursor.fetchall()

    output = "Rezultati pretrage:\n"
    for row in rows:
        output += (
            f"CustomerId: {row.CustomerId}, "
            f"Name: {row.cn}, "
            f"Code: {row.Code}, "
            f"Branch: {row.Branch}, "
            f"BlueCoatsNo: {row.BlueCoatsNo}, "
            f"PlanCurrentYear: {row.PlanCurrentYear}, "
            f"TurnoverCurrentYear: {row.TurnoverCurrentYear}, "
            f"FullfilmentCurrentYear: {row.FullfilmentCurrentYear}, "
            f"CalculatedNumberOfVisits: {row.CalculatedNumberOfVisits}, "
            f"PaymentAvgDays: {row.PaymentAvgDays}, "
            f"BalanceOutOfLimit: {row.BalanceOutOfLimit}, "
            f"BalanceCritical: {row.BalanceCritical}, "
            f"Planirani iznos po poseti: {row.PlaniraniIznosPoPoseti}, "
            f"Poslednja beleška: {row.PoslednjaBeleska}"
        )

    conn.close()


    def generate_defined_report(data: str) -> str:
        """
        Generates a structured report based on the provided customer data.

        This inner function takes a formatted string containing customer data and sends a prompt to the OpenAI API
        to generate a detailed report in Serbian. The report includes specific fields such as:
            - Naziv kupca (Customer Name)
            - Šifra kupca, naziv branše i broj plavih mantila (Customer Code, Branch Name, and Blue Coats Number)
            - Plan kupca i trenutno ostvarenje (Plan for the Customer and Current Achievement)
            - Planirani iznos po poseti i ukupan broj poseta (Planned Amount per Visit and Total Number of Visits)
            - Prosečni dani plaćanja, dugovanje izvan valute i kritični saldo (Average Payment Days, Debt Outside Currency, and Critical Balance)
            - Beleška sa prethodne posete (Note from the Previous Visit)

        The report is generated without a summary, containing only the requested data to ensure clarity and focus.

        Args:
            data (str): The formatted string containing customer data to be included in the report.

        Returns:
            str: A generated report in Serbian containing the specified customer information.
        """
        prompt = f"Generate report from the given data: {data}"
        response = client.chat.completions.create(
            model="gpt-4o",
            temperature=0.0,
            messages=[
                {
            "role": "system",
            "content": (
                """Traženi podaci za izveštaj su sledeći:
                    •   Naziv kupca (Name)
                    •	Šifra kupca, naziv branše i broj plavih mantila 
                    •	Plan kupca i trenutno ostvarenje (promet i %)
                    •	Planirani iznos po poseti, ukupan broj poseta
                    •	Prosečni dani plaćanja, dugovanje izvan valute i kritični saldo
                    •	Beleška sa prethodne posete

                    Izveštaj mora biti na srpskom jeziku.
                    Ne treba da sadrži rezime, već samo tražene podatke.
                """
            )
        },
                {"role": "user", "content": prompt}
            ]
        )
        
        return response.choices[0].message.content
    
    fin_output = generate_defined_report(output)
    return fin_output


ZA_FUNC_CALL = """

from tools import tools as yyy
def rag_tool_answer(user_query):
    client = OpenAI()

    # Tool list definition (add your tool definitions here)

    # Call the model to process the query and decide on the tool to use
    response = client.chat.completions.create(
        model=getenv("OPENAI_MODEL"),
        temperature=0.0,
        messages=[
            {"role": "system", "content": "You are a helpful assistant that chooses the most appropriate tool based on the user query. You must choose exactly one tool."},
            {"role": "user", "content": user_query}
        ],
        tools=yyy,  # Provide the tool list
        tool_choice="required"  # Allow the model to choose the tool automatically
    )

    # Check if the model made a tool call
    if response.choices[0].message.tool_calls:
        tool_call = response.choices[0].message.tool_calls[0]
        tool_name = tool_call.function.name
        tool_result = tool_call.function.arguments

        tool_arguments = json.loads(tool_result)

        if tool_name == "graphp":
            tool_result = graphp(user_query)
        elif tool_name == "hybrid_query_processor":
            processor = HybridQueryProcessor(namespace="delfi-podrska", delfi_special=1)
            tool_result = processor.process_query_results(user_query)
        elif tool_name == "SelfQueryDelfi":
            if "namespace" in tool_arguments:
                tool_result = SelfQueryDelfi(upit=tool_arguments['upit'], namespace=tool_arguments['namespace'])
            else:
                tool_result = SelfQueryDelfi(user_query)
        elif tool_name == "pineg":
            tool_result = pineg(user_query)
        elif tool_name == "order_delfi":
            tool_result = order_delfi(user_query)
        else:
            tool_result = "Tool not found or not implemented"

        return tool_result, tool_name
    else:
        # Handle cases where no tool was called, return a default response
        return "No relevant tool found", "None"

"""