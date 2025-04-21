import sqlite3
import time
import re
from datetime import datetime, timedelta
import hashlib
from textblob import TextBlob
import logging
import random
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

# Configuração de logging
logging.basicConfig(filename='sales_bot.log', level=logging.DEBUG, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

def remove_non_bmp_chars(text):
    return ''.join(char for char in text if ord(char) <= 0xFFFF)

def setup_database():
    conn = sqlite3.connect('whatsapp_sales.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            last_interaction TEXT,
            lead_score INTEGER DEFAULT 50,
            initial_message_sent BOOLEAN DEFAULT 0,
            industry TEXT,
            pain_point TEXT,
            last_follow_up TEXT,
            engagement_level TEXT DEFAULT 'neutro',
            current_stage TEXT DEFAULT 'prospecção'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER,
            message TEXT NOT NULL,
            sender TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            sentiment TEXT,
            message_hash TEXT,
            context_summary TEXT,
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales_scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL,
            keyword TEXT NOT NULL,
            response TEXT NOT NULL,
            success_count INTEGER DEFAULT 0,
            use_count INTEGER DEFAULT 0,
            tone TEXT DEFAULT 'profissional'
        )
    ''')
    
    for table, column, column_def in [
        ('contacts', 'engagement_level', 'TEXT DEFAULT "neutro"'),
        ('contacts', 'current_stage', 'TEXT DEFAULT "prospecção"'),
        ('messages', 'context_summary', 'TEXT'),
        ('sales_scripts', 'tone', 'TEXT DEFAULT "profissional"')
    ]:
        cursor.execute(f"PRAGMA table_info({table})")
        if column not in [col[1] for col in cursor.fetchall()]:
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN {column} {column_def}')
    
    # Limpar scripts antigos
    cursor.execute('DELETE FROM sales_scripts')
    
    cursor.executemany('''
        INSERT INTO sales_scripts (stage, keyword, response, tone) VALUES (?, ?, ?, ?)
    ''', [
        ('prospecção', 'oi|olá|ola', 'Olá, {contact_name}! Tudo bem? Percebi que você atua no setor de {industry} e enfrenta {pain_point}. Nosso {product} pode ajudar a resolver isso de forma prática e eficiente. Posso te contar como? 😊', 'profissional'),
        ('prospecção', 'oi|olá|ola', 'Oi, {contact_name}! Como tá indo? Soube que você trabalha com {industry} e talvez lide com {pain_point}. Nosso {product} tem soluções legais pra isso! Quer saber mais? 🚀', 'descontraído'),
        ('nurturing', 'saber|explicar|interessado|claro|ok|clr|como|adoraria|mostrar|me explique', 'Que ótimo, {contact_name}! Nosso {product} ensina estratégias comprovadas para atrair mais clientes no setor de {industry}. Por exemplo, ele mostra como criar campanhas que resolvem {pain_point}. Quer um trecho grátis? 📖', 'profissional'),
        ('nurturing', 'saber|explicar|interessado|claro|ok|clr|como|adoraria|mostrar|me explique', 'Demais, {contact_name}! O {product} tem dicas práticas pra resolver {pain_point} no {industry}. Te mando um pedacinho grátis pra você ver como é? 😄', 'descontraído'),
        ('objeção', 'caro', 'Entendo, {contact_name}. O custo pode parecer alto, mas o {product} entrega {benefit}, com retorno rápido. Temos clientes no {industry} com resultados incríveis! Quer um caso de sucesso? 📈', 'profissional'),
        ('objeção', 'tempo', 'Sei que tempo é corrido, {contact_name}! O {product} é simples e resolve {pain_point} rapidinho. Posso te mostrar como em 5 minutos? ⏱️', 'profissional'),
        ('fechamento', 'quero|comprar', 'Show, {contact_name}! 🚀 Vamos garantir seu {product} agora? Temos uma oferta especial hoje: 20% de desconto! Qual o melhor jeito de te enviar o link? 💼', 'profissional'),
        ('follow-up', 'silêncio', 'Oi, {contact_name}! Tudo certo? Lembrei de você porque nosso {product} é ideal para {pain_point}. Outros no {industry} estão vendo resultados. Quer conversar? 🌟', 'profissional')
    ])
    
    conn.commit()
    return conn, cursor

def detect_user_tone(message):
    message_lower = message.lower()
    if len(message) < 20 or any(emoji in message for emoji in ['😊', '😄', '🚀', 'haha', 'lol']):
        return 'descontraído'
    elif len(message) > 100 or re.search(r'\b(prezado|atenciosamente|obrigado)\b', message_lower):
        return 'formal'
    return 'profissional'

def analyze_sentiment(message):
    try:
        blob = TextBlob(message)
        polarity = blob.sentiment.polarity
        message_lower = message.lower()
        
        if re.search(r'\b(quero|comprar|interessado|show|legal|ótimo|valeu|adoraria)\b', message_lower):
            return "Positivo"
        if re.search(r'\b(não|caro|pare|stop|desinteressado)\b', message_lower):
            return "Negativo"
        if re.search(r'\b(saber|explicar|como|qual|detalhes|mostrar|me explique)\b', message_lower):
            return "Curioso"
        
        if polarity > 0.3:
            return "Positivo"
        elif polarity < -0.3:
            return "Negativo"
        elif 0.1 < polarity <= 0.3:
            return "Curioso"
        elif -0.3 <= polarity < -0.1:
            return "Hesitante"
        return "Neutro"
    except Exception as e:
        logging.error(f"Erro na análise de sentimento: {str(e)}")
        return "Neutro"

def summarize_context(cursor, contact_id):
    cursor.execute('SELECT message, sender FROM messages WHERE contact_id = ? ORDER BY timestamp DESC LIMIT 10', 
                  (contact_id,))
    messages = cursor.fetchall()
    summary = "Conversa recente: "
    for msg, sender in messages:
        summary += f"{sender}: {msg[:50]}... "
    return summary[:200]

def update_contact(cursor, conn, name, industry=None, pain_point=None):
    cursor.execute('SELECT id, engagement_level, current_stage FROM contacts WHERE name = ?', (name,))
    contact = cursor.fetchone()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if not contact:
        cursor.execute('''
            INSERT INTO contacts (name, last_interaction, lead_score, initial_message_sent, industry, pain_point, engagement_level, current_stage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, now, 50, 0, industry, pain_point, 'neutro', 'prospecção'))
        conn.commit()
        return cursor.lastrowid
    else:
        engagement, current_stage = contact[1], contact[2]
        cursor.execute('UPDATE contacts SET last_interaction = ?, industry = ?, pain_point = ? WHERE name = ?',
                       (now, industry, pain_point, name))
        conn.commit()
        return contact[0]

def log_message(cursor, conn, contact_id, message, sender, sentiment):
    message_hash = hashlib.sha256(message.encode()).hexdigest()
    context_summary = summarize_context(cursor, contact_id)
    
    cursor.execute('SELECT id FROM messages WHERE contact_id = ? AND message_hash = ?', (contact_id, message_hash))
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO messages (contact_id, message, message_hash, sender, timestamp, sentiment, context_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (contact_id, message, message_hash, sender, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), sentiment, context_summary))
        conn.commit()

def get_sales_script(cursor, message, stage, contact_id, contact_name, product, pain_point=None, industry=None):
    message_lower = message.lower()
    user_tone = detect_user_tone(message)
    
    cursor.execute('SELECT response, id, keyword, tone FROM sales_scripts WHERE stage = ?', (stage,))
    scripts = cursor.fetchall()
    
    for response, script_id, keyword, tone in scripts:
        if re.search(rf'\b{keyword}\b', message_lower) and tone in (user_tone, 'profissional'):
            benefit = f"técnicas para superar {pain_point}" if pain_point else "resultados rápidos"
            pain_point = pain_point or "seus desafios"
            industry = industry or "seu setor"
            
            formatted_response = response.format(
                contact_name=contact_name, 
                product=product, 
                benefit=benefit, 
                pain_point=pain_point, 
                industry=industry
            )
            return formatted_response, script_id
    
    default_response = f"Entendi, {contact_name}! Parece que você está interessado em resolver {pain_point or 'seus desafios'} no {industry or 'seu setor'}. Nosso {product} tem estratégias específicas para isso. Quer que eu explique mais ou envie um trecho grátis? 😊"
    return default_response, None

def mark_script_success(cursor, conn, script_id):
    if script_id:
        cursor.execute('UPDATE sales_scripts SET success_count = success_count + 1 WHERE id = ?', (script_id,))
        conn.commit()

def train_ai(cursor, conn):
    print("\nModo de Treinamento da IA")
    stage = input("Estágio do funil (prospecção, objeção, fechamento, nurturing, follow-up): ").strip().lower()
    keyword = input("Palavra-chave para acionar a resposta: ").strip().lower()
    response = input("Resposta ideal (use {contact_name}, {product}, {benefit}, {pain_point}, {industry}): ").strip()
    tone = input("Tom da resposta (profissional, descontraído, formal): ").strip().lower() or 'profissional'
    
    cursor.execute('INSERT INTO sales_scripts (stage, keyword, response, tone) VALUES (?, ?, ?, ?)', 
                   (stage, keyword, response, tone))
    conn.commit()
    print("Treinamento salvo com sucesso!")

def check_follow_ups(cursor, conn, driver, product):
    now = datetime.now()
    cursor.execute('''
        SELECT id, name, last_interaction, pain_point, industry
        FROM contacts
        WHERE last_follow_up IS NULL OR last_follow_up < ?
    ''', ((now - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),))
    
    for contact_id, name, last_interaction, pain_point, industry in cursor.fetchall():
        last_interaction_time = datetime.strptime(last_interaction, '%Y-%m-%d %H:%M:%S')
        if now - last_interaction_time > timedelta(hours=48):
            response, script_id = get_sales_script(cursor, 'silêncio', 'follow-up', contact_id, name, product, pain_point, industry)
            if response:
                send_message(driver, cursor, conn, contact_id, name, response)
                cursor.execute('UPDATE contacts SET last_follow_up = ? WHERE id = ?',
                               (now.strftime('%Y-%m-%d %H:%M:%S'), contact_id))
                conn.commit()

def read_messages(driver, cursor, conn, contact_id, contact_name, product, pain_point=None, industry=None):
    try:
        # Aguardar a lista de conversas
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, '//div[@aria-label="Lista de conversas"]'))
        )
        
        # Tentar abrir a conversa do contato
        try:
            contact_element = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, f'//span[@title="{contact_name}"]'))
            )
            contact_element.click()
            time.sleep(7)
        except:
            logging.warning(f"Conversa com {contact_name} não encontrada. Iniciando nova conversa.")
            response, script_id = get_sales_script(cursor, 'oi', 'prospecção', contact_id, contact_name, product, pain_point, industry)
            if send_message(driver, cursor, conn, contact_id, contact_name, response):
                cursor.execute('UPDATE contacts SET initial_message_sent = 1, last_interaction = ? WHERE id = ?',
                               (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), contact_id))
                conn.commit()
            return False

        # Monitorar mensagens novas por até 120 segundos
        start_time = time.time()
        timeout = 120
        new_messages = False
        
        while time.time() - start_time < timeout:
            try:
                # Rolar para o final da conversa
                message_pane = driver.find_element(By.XPATH, '//div[@id="main"]')
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", message_pane)
                time.sleep(7)
                logging.info(f"Verificando mensagens para {contact_name}")

                # Tentar diferentes XPaths para mensagens recebidas
                xpaths = [
                    '//div[contains(@class, "message-in")]//span[@dir="ltr"]',
                    '//div[contains(@class, "message-in")]//div[@data-pre-plain-text]//span',
                    '//div[contains(@class, "message-in")]//span[contains(@class, "selectable-text")]'
                ]
                messages = []
                for xpath in xpaths:
                    try:
                        messages = WebDriverWait(driver, 10).until(
                            EC.presence_of_all_elements_located((By.XPATH, xpath))
                        )
                        if messages:
                            logging.info(f"XPath bem-sucedido: {xpath}, encontradas {len(messages)} mensagens para {contact_name}")
                            break
                    except:
                        logging.warning(f"XPath falhou: {xpath} para {contact_name}")
                        continue
                
                if not messages:
                    logging.error(f"Nenhuma mensagem encontrada para {contact_name} com qualquer XPath")
                    time.sleep(7)
                    continue
                
                # Processar as últimas mensagens
                for msg in messages[-2:]:
                    try:
                        clean_msg = remove_non_bmp_chars(msg.text.strip())
                        if clean_msg:
                            message_hash = hashlib.sha256(clean_msg.encode()).hexdigest()
                            cursor.execute('SELECT id FROM messages WHERE contact_id = ? AND message_hash = ?', (contact_id, message_hash))
                            
                            if not cursor.fetchone():
                                sentiment = analyze_sentiment(clean_msg)
                                log_message(cursor, conn, contact_id, clean_msg, 'user', sentiment)
                                print(f"\nNova mensagem de {contact_name}: {clean_msg} (Sentimento: {sentiment})")
                                logging.info(f"Nova mensagem de {contact_name}: {clean_msg} (Sentimento: {sentiment})")
                                
                                engagement = 'positivo' if sentiment in ['Positivo', 'Curioso'] else 'negativo' if sentiment == 'Negativo' else 'neutro'
                                new_stage = 'nurturing' if sentiment in ['Positivo', 'Curioso'] else 'objeção' if sentiment == 'Negativo' else 'prospecção'
                                
                                cursor.execute('UPDATE contacts SET engagement_level = ?, current_stage = ?, last_interaction = ?, initial_message_sent = 1 WHERE id = ?', 
                                               (engagement, new_stage, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), contact_id))
                                conn.commit()
                                
                                new_messages = True
                                
                                cursor.execute('SELECT current_stage FROM contacts WHERE id = ?', (contact_id,))
                                current_stage = cursor.fetchone()[0]
                                
                                response, script_id = get_sales_script(cursor, clean_msg, current_stage, contact_id, contact_name, product, pain_point, industry)
                                if response:
                                    time.sleep(random.uniform(2, 4))
                                    send_message(driver, cursor, conn, contact_id, contact_name, response)
                                    
                                    if sentiment in ["Positivo", "Curioso"]:
                                        mark_script_success(cursor, conn, script_id)
                                    
                                    lead_score_change = 15 if sentiment in ["Positivo", "Curioso"] else -10 if sentiment == "Negativo" else 0
                                    cursor.execute('UPDATE contacts SET lead_score = lead_score + ? WHERE id = ?', 
                                                   (lead_score_change, contact_id))
                                    conn.commit()
                                
                                if re.search(r'\b(não|pare|stop|desinteressado)\b', clean_msg.lower()):
                                    send_message(driver, cursor, conn, contact_id, contact_name,
                                                 f"Entendido, {contact_name}. Respeito sua decisão. Caso queira conversar no futuro, é só me chamar! 😊")
                                    cursor.execute('UPDATE contacts SET lead_score = 0, engagement_level = "negativo", current_stage = "opt-out" WHERE id = ?', 
                                                   (contact_id,))
                                    conn.commit()
                                    return True
                    except Exception as e:
                        logging.error(f"Erro ao processar mensagem para {contact_name}: {str(e)}")
                        driver.save_screenshot(f"erro_process_message_{contact_name}_{int(time.time())}.png")
                        continue
                
                if new_messages:
                    break
                
                time.sleep(7)
            
            except Exception as e:
                logging.error(f"Erro ao ler mensagens para {contact_name} durante monitoramento: {str(e)}")
                driver.save_screenshot(f"erro_read_messages_loop_{contact_name}_{int(time.time())}.png")
                time.sleep(7)
        
        return new_messages

    except Exception as e:
        logging.error(f"Erro ao iniciar leitura de mensagens para {contact_name}: {str(e)}")
        driver.save_screenshot(f"erro_read_messages_{contact_name}_{int(time.time())}.png")
        return False

def send_message(driver, cursor, conn, contact_id, contact_name, message):
    retries = 3
    for attempt in range(retries):
        try:
            search_box = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, '//div[@contenteditable="true"][@data-tab="3"]'))
            )
            search_box.click()
            search_box.send_keys(Keys.CONTROL + "a")
            search_box.send_keys(Keys.DELETE)
            search_box.send_keys(contact_name)
            time.sleep(7)

            contact = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, f'//span[@title="{contact_name}"]'))
            )
            contact.click()
            time.sleep(7)

            msg_box = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.XPATH, '//div[@contenteditable="true"][@data-tab="10"]'))
            )
            
            msg_box.click()
            time.sleep(1)
            clean_message = remove_non_bmp_chars(message)
            
            for char in clean_message:
                msg_box.send_keys(char)
                time.sleep(random.uniform(0.05, 0.15))
            
            time.sleep(1)
            msg_box.send_keys(Keys.ENTER)
            print(f"\n➡️ Mensagem enviada para {contact_name}: '{clean_message}'")
            logging.info(f"Mensagem enviada para {contact_name}: {clean_message}")
            
            sentiment = analyze_sentiment(clean_message)
            log_message(cursor, conn, contact_id, clean_message, 'bot', sentiment)
            
            logging.info(f"Atualizando initial_message_sent para {contact_name}")
            time.sleep(random.uniform(2, 4))
            return True
        except Exception as e:
            logging.error(f"Tentativa {attempt+1}/{retries} falhou ao enviar mensagem para {contact_name}: {str(e)}")
            driver.save_screenshot(f"erro_send_message_{contact_name}_{int(time.time())}.png")
            time.sleep(7)
    logging.error(f"Falha ao enviar mensagem para {contact_name} após {retries} tentativas")
    return False

def generate_analytics(cursor):
    print("\n📊 Relatório de Contatos:")
    cursor.execute('SELECT name, lead_score, engagement_level, current_stage FROM contacts ORDER BY lead_score DESC')
    for name, score, engagement, stage in cursor.fetchall():
        print(f"{name}: Score={score}, Engajamento={engagement}, Estágio={stage}")
    
    print("\n📈 Desempenho dos Scripts:")
    cursor.execute('SELECT stage, keyword, success_count, use_count FROM sales_scripts WHERE use_count > 0')
    for stage, keyword, success, use in cursor.fetchall():
        rate = (success / use * 100) if use > 0 else 0
        print(f"{stage} ({keyword}): {rate:.1f}% de sucesso ({success}/{use})")

def main():
    conn, cursor = setup_database()
    
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-notifications")
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    driver = webdriver.Chrome(options=options)
    
    try:
        print("\n🔗 Acessando WhatsApp Web...")
        driver.get("https://web.whatsapp.com/")
        WebDriverWait(driver, 120).until(
            EC.presence_of_element_located((By.XPATH, '//div[@aria-label="Lista de conversas"]'))
        )
        print("✅ Login realizado com sucesso!")
        
        product = input("\n📝 Qual produto/serviço você está vendendo? (Ex: Ebook de Marketing Digital): ").strip()
        if not product:
            product = "Ebook de Marketing Digital"
        
        if input("\n🧠 Deseja adicionar novos scripts de resposta? (s/n): ").lower() == 's':
            train_ai(cursor, conn)
        
        print("\n👥 Cadastro de Contatos (digite 'sair' para terminar):")
        print("Formato: Nome;Indústria;Ponto de Dor")
        print("Exemplo: João Silva;Varejo;Falta de clientes")
        
        contacts = []
        while True:
            contact_input = input("Contato: ").strip()
            if contact_input.lower() == 'sair':
                break
            if contact_input:
                parts = contact_input.split(';')
                name = parts[0].strip()
                industry = parts[1].strip() if len(parts) > 1 else None
                pain_point = parts[2].strip() if len(parts) > 2 else None
                if name:
                    contacts.append((name, industry, pain_point))
        
        if not contacts:
            print("⚠️ Nenhum contato cadastrado. Encerrando...")
            return
        
        print("\n🤖 Iniciando atendimento automático...")
        while True:
            try:
                for name, industry, pain_point in contacts:
                    contact_id = update_contact(cursor, conn, name, industry, pain_point)
                    
                    cursor.execute('SELECT initial_message_sent FROM contacts WHERE id = ?', (contact_id,))
                    initial_sent = cursor.fetchone()[0]
                    
                    if not initial_sent:
                        response, script_id = get_sales_script(cursor, 'oi', 'prospecção', contact_id, name, product, pain_point, industry)
                        if response:
                            if send_message(driver, cursor, conn, contact_id, name, response):
                                cursor.execute('UPDATE contacts SET initial_message_sent = 1, last_interaction = ? WHERE id = ?',
                                               (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), contact_id))
                                conn.commit()
                                logging.info(f"Mensagem inicial enviada para {name}")
                    
                    if read_messages(driver, cursor, conn, contact_id, name, product, pain_point, industry):
                        time.sleep(15)
                    
                    check_follow_ups(cursor, conn, driver, product)
                    
                    time.sleep(7)
                
                generate_analytics(cursor)
                time.sleep(10)  # Pausa entre ciclos, sem entrada do usuário
            
            except Exception as e:
                logging.error(f"Erro no loop principal: {str(e)}")
                driver.save_screenshot(f"erro_main_loop_{int(time.time())}.png")
                print(f"\n⚠️ Erro no loop principal: {str(e)}. Continuando após 15 segundos...")
                time.sleep(15)
                continue
        
    except Exception as e:
        print(f"\n❌ Erro durante a execução: {str(e)}")
        logging.error(f"Erro principal: {str(e)}")
        driver.save_screenshot(f"erro_main_{int(time.time())}.png")
    finally:
        driver.quit()
        conn.close()
        print("\n✅ Programa encerrado. Navegador e banco de dados fechados.")

if __name__ == "__main__":
    main()