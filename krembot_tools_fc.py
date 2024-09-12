import json
import neo4j
import pyodbc
import requests
import streamlit as st
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
from typing import List, Dict

from krembot_db import work_prompts

mprompts = work_prompts()
client = OpenAI(api_key=getenv("OPENAI_API_KEY"))

def connect_to_neo4j():
    return neo4j.GraphDatabase.driver(getenv("NEO4J_URI"), auth=(getenv("NEO4J_USER"), getenv("NEO4J_PASS")))


def connect_to_pinecone(x):
    pinecone_api_key = getenv('PINECONE_API_KEY')
    pinecone_host = "https://delfi-a9w1e6k.svc.aped-4627-b74a.pinecone.io" if x == 0 else "https://neo-positive-a9w1e6k.svc.apw5-4e34-81fa.pinecone.io"
    return Pinecone(api_key=pinecone_api_key, host=pinecone_host).Index(host=pinecone_host)


from tools import tools as yyy
def rag_tool_answer(user_query):
    """
    Processes a user query by utilizing the appropriate tool selected by the OpenAI model.

    This function sends a user query to the AI model, which decides which tool to use.
    The tool is called, and the response is returned.

    Parameters:
    - user_query: The user's query.

    Returns:
    - The result from the selected tool and the tool name.
    """
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


def rag_tool_answer2(prompt):
    st.session_state.rag_tool = "ClientDirect"

    if os.getenv("APP_ID") == "InteliBot":
        return intelisale(prompt), st.session_state.rag_tool

    elif os.getenv("APP_ID") == "DentyBot":
        return dentyWF(prompt), st.session_state.rag_tool
        
    context = " "
    st.session_state.rag_tool = get_structured_decision_from_model(prompt)

    if st.session_state.rag_tool == "Hybrid":
        processor = HybridQueryProcessor(namespace="delfi-podrska", delfi_special=1)
        context = processor.process_query_results(prompt)

    elif st.session_state.rag_tool == "Opisi":
        uvod = mprompts["rag_self_query"]
        prompt = uvod + prompt
        context = SelfQueryDelfi(prompt)

    elif st.session_state.rag_tool == "Korice":
        uvod = mprompts["rag_self_query"]
        prompt = uvod + prompt
        context = SelfQueryDelfi(upit=prompt, namespace="korice")
        
    elif st.session_state.rag_tool == "Graphp": 
        context = graphp(prompt)

    elif st.session_state.rag_tool == "Pineg":
        context = pineg(prompt)

    elif st.session_state.rag_tool == "Orders":
        context = order_delfi(prompt)

    elif st.session_state.rag_tool == "FAQ":
        processor = HybridQueryProcessor(namespace="ecd-faq", delfi_special=1)
        context = processor.process_query_results(prompt)
        
    elif st.session_state.rag_tool == "Uputstva":
        processor = HybridQueryProcessor(namespace="ecd-uputstva", delfi_special=1)
        context = processor.process_query_results(prompt)

    elif st.session_state.rag_tool == "Blogovi":
        processor = HybridQueryProcessor(namespace="ecd-blogovi", delfi_special=1)
        context = processor.process_query_results(prompt)

    return context, st.session_state.rag_tool


def get_structured_decision_from_model(user_query):
    """
    Determines the most appropriate tool to use for a given user query using an AI model.

    This function sends a user query to an AI model and receives a structured decision in the
    form of a JSON object. The decision includes the recommended tool to use for addressing
    the user's query, based on the content and context of the query. The function uses a
    structured prompt, generated by `create_structured_prompt`, to instruct the AI on how
    to process the query. The AI's response is parsed to extract the tool recommendation.

    Parameters:
    - user_query: The user's query for which the tool recommendation is sought.

    Returns:
    - The name of the recommended tool as a string, based on the AI's analysis of the user query.
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


def dentyWF(prompt):
    index = connect_to_pinecone(x=0)

    def get_embedding(text, model="text-embedding-3-large"):
        response = client.embeddings.create(
            input=[text],
            model=model
        ).data[0].embedding
        
        return response

    def dense_query(query, top_k, filter, namespace="servis"):
        # Get embedding for the query
        dense = get_embedding(text=query)

        query_params = {
            'top_k': top_k,
            'vector': dense,
            'include_metadata': True,
            'filter': filter,
            'namespace': namespace
        }

        response = index.query(**query_params)

        matches = response.to_dict().get('matches', [])
        return matches

    def search_pinecone_second_set(device: str) -> List[Dict]:
        # Define the query text and filter for the new metadata structure
        query = "Find device"
        filter = {"device": {"$eq": device}}
        
        query_embedding_2 = dense_query(query, top_k=5, filter=filter)
        
        # Extract metadata and map it to the new structure
        matches = []
        for match in query_embedding_2:
            metadata = match['metadata']
            matches.append({
                'url': metadata['url'],
                'text': metadata['text'],
                'device': metadata['device'],
            })
        
        return matches
    
    denty_tools = "T3T4 Racer, ORTHOPHOS XG 3, SIROTorque L+, inEos X5, inLab MC X5, M1+C2+, TENEO, SIVISION 3, Sivision Digital"
    denty_tools_2 = ["T3T4 Racer", "ORTHOPHOS XG 3", "SIROTorque L+", "inEos X5", "inLab MC X5", "M1+C2+", "TENEO", "SIVISION 3", "Sivision Digital"]
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.0,
        messages=[
            {"role": "system", "content": f"""
                You are a helpful assistant that chooses the most appropriate tool based on a given user query. Your output is only the tool name.
                These are the possible tools: {denty_tools}
            """},
            {"role": "user", "content": prompt}
        ]
    )

    device = response.choices[0].message.content.strip()
    print(3333, device)
    if device not in denty_tools_2:
        return "Niste uneli ispravno ime uređaja. Molimo pokušajte ponovo.", "Denty"
    
    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.0,
        messages=[
            {"role": "system", "content": f"You are a helpful assistant that chooses the most appropriate answer(s) from the provided context, for the given user query. Only use the provided context (it's included the user message) to generate the answer. The context is about the device: {device}"},
            {"role": "user", "content": f"User query: {prompt}, /n/n context: {search_pinecone_second_set(device)}"}
        ]
    )
    return response.choices[0].message.content.strip(), "DentyBot"


def graphp(pitanje):
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
                "Ensure to include a condition to check that the quantity property of Book nodes is greater than 0 to ensure the books are in stock where this filter is plausable."
                "When writing the Cypher query, ensure that instead of '=' use CONTAINS, in order to return all items which contains the searched term."
                "When generating the Cypher query, ensure to handle inflected forms properly by converting all names to their nominative form. For example, if the user asks for books by 'Adrijana Čajkovskog,' the query should be generated for 'Adrijan Čajkovski,' ensuring that the search is performed using the base form of the author's name."
                "When returning some properties of books, ensure to always return the oldProductId and the title too."
                "Ensure to limit the number of records returned to 10."

                "Here is an example user question and the corresponding Cypher query: "
                "Example user question: 'Pronađi knjigu Da Vinčijev kod.' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Da Vinčijev kod') AND b.quantity > 0 RETURN b LIMIT 10"

                "Example user question: 'O čemu se radi u knjizi Memoari jedne gejše?' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Memoari jedne gejše') RETURN b LIMIT 10"

                "Example user question: 'Interesuje me knjiga Piramide.' "
                "Cypher query: MATCH (b:Book)-[:WROTE]-(a:Author) WHERE toLower(b.title) CONTAINS toLower('Piramide') AND b.quantity > 0 RETURN b.title AS title, b.oldProductId AS oldProductId, b.category AS category, a.name AS author LIMIT 10"
                
                "Example user question: 'Preporuci mi knjige istog žanra kao Krhotine.' "
                "Cypher query: MATCH (b:Book)-[:BELONGS_TO]->(g:Genre) WHERE toLower(b.title) CONTAINS toLower('Krhotine') WITH g MATCH (rec:Book)-[:BELONGS_TO]->(g)<-[:BELONGS_TO]-(b:Book) WHERE b.title CONTAINS 'Krhotine' AND rec.quantity > 0 MATCH (rec)-[:WROTE]-(a:Author) RETURN rec.title AS title, rec.oldProductId AS oldProductId, b.category AS category, a.name AS author, g.name AS genre LIMIT 10"

                "Example user question: 'Koja je cena za Autostoperski vodič kroz galaksiju?' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Autostoperski vodič kroz galaksiju') AND b.quantity > 0 RETURN b.title AS title, b.oldProductId AS oldProductId, b.category AS category LIMIT 10"

                "Example user question: 'Da li imate anu karenjinu na stanju' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Ana Karenjina') AND b.quantity > 0 RETURN b.title AS title, b.oldProductId AS oldProductId, b.category AS category LIMIT 10"

                "Example user question: 'Intresuju me fantastika. Preporuči mi neke knjige' "
                "Cypher query: MATCH (a:Author)-[:WROTE]->(b:Book)-[:BELONGS_TO]->(g:Genre {name: 'Fantastika'}) RETURN b, a.name, g.name LIMIT 10"
                
                "Example user question: 'Da li imate mobi dik na stanju, treba mi 27 komada?' "
                "Cypher query: MATCH (b:Book) WHERE toLower(b.title) CONTAINS toLower('Mobi Dik') AND b.quantity > 27 RETURN b.title AS title, b.quantity AS quantity, b.oldProductId AS oldProductId, b.category AS category LIMIT 10"
            
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

        return matches

    def search_pinecone(query: str) -> List[Dict]:
        # Dobij embedding za query
        query_embedding = dense_query(query, top_k=15, filter=None)
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
        query_embedding_2 = dense_query(query, top_k=10, filter=filter)
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
                
                combined_results.append(combined_data)
                # print(f"Combined Results: {combined_results}")
                
                
                # return display_results(combined_data)
            else:
                break
    display_results(combined_data)
    # print(f"Combined Results: {combined_results}")
    # print(f"Display Results: {display_results(combined_results)}")
    return combined_results

def API_search_2(order_ids):

    def get_order_info(order_id):
        url = f"http://185.22.145.64:3003/api/order-info/{order_id}"
        headers = {
            'x-api-key': getenv("DELFI_ORDER_API_KEY")
        }
        return requests.get(url, headers=headers).json()

    # Function to parse the JSON response and extract required fields
    def parse_order_info(json_data):
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
    def get_multiple_orders_info(order_ids):
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

    return orders_info


import re
def order_delfi(prompt):
    def extract_orders_from_string(text):
        # Define a regular expression pattern to match 5 or more digit integers
        pattern = r'\b\d{5,}\b'
        
        # Use re.findall to extract all matching patterns
        orders = re.findall(pattern, text)
        
        # Convert the matched strings to integers
        orders = [int(order) for order in orders]
    order_ids = extract_orders_from_string(prompt)
    if len(order_ids) > 0:
        return API_search_2(order_ids)
    else:
        return "Morate uneti tačan broj porudžbine/a."


def API_search(matching_sec_ids):

    def get_product_info(token, product_id):
        return requests.get(url="https://www.delfi.rs/api/products", params={"token": token, "product_id": product_id}).content

    # Function to parse the XML response and extract required fields
    def parse_product_info(xml_data):
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
                    elif type == "quantityDiscount" or type == "quantityDiscount2":
                        title = action_node.find('title').text
                        start_at = action_node.find('startAt').text
                        end_at = action_node.find('endAt').text
                        price_quantity_standard_d2 = float(action_node.find('priceQuantityStandard').text)
                        price_quantity_premium_d2 = float(action_node.find('priceQuantityPremium').text)
                        quantity_discount_limit = int(action_node.find('quantityDiscountLimit').text)

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
    def get_multiple_products_info(token, product_ids):
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


def SelfQueryDelfi(upit, api_key=None, environment=None, index_name='delfi', namespace='opisi', openai_api_key=None, host=None):
    """
    Executes a query against a Pinecone vector database using specified parameters or environment variables. 
    The function initializes the Pinecone and OpenAI services, sets up the vector store and metadata, 
    and performs a query using a custom retriever based on the provided input 'upit'.

    It is used for self-query on metadata.

    Parameters:
    upit (str): The query input for retrieving relevant documents.
    api_key (str, optional): API key for Pinecone. Defaults to PINECONE_API_KEY from environment variables.
    environment (str, optional): Pinecone environment. Defaults to PINECONE_API_KEY from environment variables.
    index_name (str, optional): Name of the Pinecone index to use. Defaults to 'positive'.
    namespace (str, optional): Namespace for Pinecone index. Defaults to NAMESPACE from environment variables.
    openai_api_key (str, optional): OpenAI API key. Defaults to OPENAI_API_KEY from environment variables.

    Returns:
    str: A string containing the concatenated results from the query, with each document's metadata and content.
         In case of an exception, it returns the exception message.

    Note:
    The function is tailored to a specific use case involving Pinecone and OpenAI services. 
    It requires proper setup of these services and relevant environment variables.
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

    Attributes:
        api_key (str): The API key for Pinecone.
        environment (str): The Pinecone environment setting.
        alpha (float): The weight used to balance dense and sparse vector scores.
        score (float): The score treshold.
        index_name (str): The name of the Pinecone index to be used.
        index: The Pinecone index object.
        namespace (str): The namespace to be used for the Pinecone index.
        top_k (int): The number of results to be returned.
            
    Example usage:
    processor = HybridQueryProcessor(api_key=environ["PINECONE_API_KEY"], 
                                 environment=environ["PINECONE_API_KEY"],
                                 alpha=0.7, 
                                 score=0.35,
                                 index_name='custom_index'), 
                                 namespace=environ["NAMESPACE"],
                                 top_k = 10 # all params are optional

    result = processor.hybrid_query("some query text")    
    """

    def __init__(self, **kwargs):
        """
        Initializes the HybridQueryProcessor with optional parameters.

        The API key and environment settings are fetched from the environment variables.
        Optional parameters can be passed to override these settings.

        Args:
            **kwargs: Optional keyword arguments:
                - api_key (str): The API key for Pinecone (default fetched from environment variable).
                - environment (str): The Pinecone environment setting (default fetched from environment variable).
                - alpha (float): Weight for balancing dense and sparse scores (default 0.5).
                - score (float): Weight for balancing dense and sparse scores (default 0.05).
                - index_name (str): Name of the Pinecone index to be used (default 'positive').
                - namespace (str): The namespace to be used for the Pinecone index (default fetched from environment variable).
                - top_k (int): The number of results to be returned (default 6).
        """
        self.api_key = kwargs.get('api_key', getenv('PINECONE_API_KEY'))
        self.environment = kwargs.get('environment', getenv('PINECONE_API_KEY'))
        self.alpha = kwargs.get('alpha', 0.5)  # Default alpha is 0.5
        self.score = kwargs.get('score', 0.05)  # Default score is 0.05
        self.index_name = kwargs.get('index', 'neo-positive')  # Default index is 'positive'
        self.namespace = kwargs.get('namespace', getenv("NAMESPACE"))  
        self.top_k = kwargs.get('top_k', 6)  # Default top_k is 6
        self.delfi_special = kwargs.get('delfi_special')
        self.index = connect_to_pinecone(self.delfi_special)
        self.host = getenv("PINECONE_HOST")

    def hybrid_score_norm(self, dense, sparse):
        """
        Normalizes the scores from dense and sparse vectors using the alpha value.

        Args:
            dense (list): The dense vector scores.
            sparse (dict): The sparse vector scores.

        Returns:
            tuple: Normalized dense and sparse vector scores.
        """
        return ([v * self.alpha for v in dense], 
                {"indices": sparse["indices"], 
                 "values": [v * (1 - self.alpha) for v in sparse["values"]]})
    
    def hybrid_query(self, upit, top_k=None, filter=None, namespace=None):
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

        for match in matches:
            try:
                metadata = match.get('metadata', {})

                # Create the result entry with all metadata fields
                result_entry = metadata.copy()

                # Ensure mandatory fields exist with default values if they are not in metadata
                result_entry.setdefault('context', '')
                result_entry.setdefault('chunk', None)
                result_entry.setdefault('source', None)
                result_entry.setdefault('score', match.get('score', 0))

                # Only add to results if 'context' exists
                if result_entry['context']:
                    results.append(result_entry)
            except Exception as e:
                # Log or handle the exception if needed
                print(f"An error occurred: {e}")
                pass

        return results
       
    def process_query_results(self, upit, dict=False):
        """
        Processes the query results and prompt tokens based on relevance score and formats them for a chat or dialogue system.
        Additionally, returns a list of scores for items that meet the score threshold.
        """
        tematika = self.hybrid_query(upit)
        if not dict:
            uk_teme = ""
            
            for item in tematika:
                if item["score"] > self.score:
                    # Build the metadata string from all relevant fields
                    metadata_str = "\n".join(f"{key}: {value}" for key, value in item.items())
                    # Append the formatted metadata string to uk_teme
                    uk_teme += metadata_str + "\n\n"
            
            return uk_teme
        else:
            return tematika
        
    def get_embedding(self, text, model="text-embedding-3-large"):

        """
        Retrieves the embedding for the given text using the specified model.

        Args:
            text (str): The text to be embedded.
            model (str): The model to be used for embedding. Default is "text-embedding-3-large".

        Returns:
            list: The embedding vector of the given text.
            int: The number of prompt tokens used.
        """
        
        text = text.replace("\n", " ")
        result = client.embeddings.create(input=[text], model=model).data[0].embedding
       
        return result


def intelisale(query):
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


    def generate_defined_report(data):
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