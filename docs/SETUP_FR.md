# Mise en ligne : de zéro à un agent qui décroche (≈ 2 h)

Ordre conseillé. Chaque étape est testable seule. Les étapes 3 et 4 sont optionnelles pour une première démo : sans Twilio les SMS sont simulés dans les logs, sans Google l'agenda reste dans PostgreSQL.

## 0. Pousser le code sur ton GitHub

1. Crée un repo public vide sur https://github.com/new, nom : `ai-voice-receptionist` (sans README).
2. Depuis le dossier du projet :

```bash
git init -b main
git add .
git commit -m "AI voice receptionist: Vapi + FastAPI + PostgreSQL + Google Calendar + Twilio"
git remote add origin https://github.com/Dioumadev221/ai-voice-receptionist.git
git push -u origin main
```

Le workflow `.github/workflows/ci.yml` lance les tests (SQLite + PostgreSQL) et le build Docker à chaque push : le badge vert sur le repo rassure les clients qui regardent ton code.

## 1. Déployer le backend (Render, ~13 $/mois)

1. Compte sur https://render.com, connecte ton GitHub.
2. **New → Blueprint**, choisis le repo. Render lit `render.yaml` et crée : l'API, la base PostgreSQL, le cron de rappels.
3. Il te demande les variables marquées `sync: false`. Pour l'instant remplis seulement `CLINIC_PHONE` (ton numéro au format `+33...` ou `+221...`, sert au transfert vers un humain). Laisse le reste vide.
4. Une fois déployé, note l'URL (ex. `https://voice-receptionist.onrender.com`). Vérifie `https://.../healthz` → `{"ok":true}`.
5. Dans l'onglet **Environment** du service web, copie la valeur générée de `VAPI_SECRET` et `ADMIN_PASSWORD`.
6. Dans le service cron `voice-reminders`, mets `APP_URL` = l'URL du point 4.
7. Données de démo pour le dashboard : onglet **Shell** du service web → `python scripts/seed_demo.py`. Puis ouvre l'URL, login `admin` / `ADMIN_PASSWORD`.

> Pourquoi pas le plan gratuit : il s'endort après 15 min, le premier appel met 30-50 s à réveiller le serveur, Vapi coupe l'outil à 20 s. Pour une démo ponctuelle tu peux tester en gratuit, pas pour un client.

## 2. Vapi (la voix)

1. Compte sur https://dashboard.vapi.ai (crédit offert à l'inscription).
2. **Credential pour sécuriser le webhook** : Settings → Integrations → Server Configuration → *Add Custom Credential* → type **Bearer Token**, Token = ton `VAPI_SECRET`, Header `Authorization`, Bearer prefix activé. Copie son ID.
3. Récupère ta **Private API key** (Org settings → API Keys) et ta **Public key**.
4. Sur ta machine, dans le dossier du projet :

```bash
pip install -r requirements.txt
export VAPI_API_KEY=<private key>  VAPI_SECRET=<le secret Render>  VAPI_CREDENTIAL_ID=<id du credential>
export CLINIC_PHONE=<ton numéro>
python scripts/vapi_setup.py --server-url https://voice-receptionist.onrender.com
```

   Le script affiche `Assistant ready: <id>`. Ajoute `VAPI_ASSISTANT_ID=<id>` dans ton environnement local pour les mises à jour suivantes (le script fait alors un PATCH au lieu de créer un doublon).
5. **Test immédiat** : dans le dashboard Vapi, ouvre l'assistant → bouton *Talk to Assistant*. Demande un détartrage ("cleaning") mardi prochain. Dans Vapi → Logs → Webhooks tu vois les appels à ton API, et le RDV apparaît dans ton dashboard.
6. **Page de démo publique** : dans Render, mets `VAPI_PUBLIC_KEY` et `VAPI_ASSISTANT_ID`. La page `https://.../demo` permet à n'importe quel prospect d'appeler Ava depuis son navigateur. C'est ce lien que tu mets sur Upwork.
7. **Numéro de téléphone** : Vapi → Phone Numbers → *Create* (numéro US gratuit Vapi) ou *Import* un numéro Twilio. Assigne l'assistant. Appelle-le.

## 3. Twilio (SMS de confirmation et rappels)

1. Compte sur https://www.twilio.com/try-twilio. En compte d'essai, les SMS ne partent que vers les numéros vérifiés : vérifie ton propre portable (Console → Phone Numbers → Verified Caller IDs). Suffisant pour filmer la démo.
2. Console → Phone Numbers → *Buy a number* (US, avec capacité SMS, ~1,15 $/mois, payé sur le crédit d'essai).
3. Dans Render, renseigne `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` (page d'accueil de la console), `TWILIO_FROM_NUMBER` (`+1...`). Mets ton portable dans `STAFF_ALERT_PHONE` pour recevoir les alertes urgences et leads.
4. Pour un vrai client US : il faudra l'enregistrement **A2P 10DLC** (Messaging → Regulatory Compliance) au nom de la clinique, ou un numéro toll-free vérifié. Compte quelques jours. C'est le client qui fournit les infos de son entreprise. Prévois-le dans ton planning de mission.

## 4. Google Calendar (synchro agenda)

1. https://console.cloud.google.com → nouveau projet → *APIs & Services → Library* → active **Google Calendar API**.
2. *IAM & Admin → Service Accounts* → *Create*, nom `voice-receptionist`, pas de rôle. Ouvre-le → *Keys → Add key → JSON*. Un fichier se télécharge.
3. Dans Google Calendar (ton compte ou celui de la clinique) : crée un agenda "Clinique", *Paramètres → Partager avec des personnes* → ajoute l'email du service account (`...@...iam.gserviceaccount.com`) avec **Modifier les événements**. Copie l'**ID de l'agenda** (section *Intégrer l'agenda*).
4. Dans Render : `GOOGLE_CALENDAR_ID` = cet ID, `GOOGLE_SERVICE_ACCOUNT_JSON` = le contenu du fichier JSON collé sur une ligne.
5. Test : réserve via l'agent → l'événement apparaît. Crée toi-même un événement "Réunion" mardi 10h-11h → l'agent ne propose plus ce créneau.

## 5. La vidéo qui vend (le plus important pour Upwork)

Enregistre 90 secondes avec Loom, écran partagé en deux : la page `/demo` à gauche, le dashboard à droite.
1. Tu demandes un RDV de détartrage "next Tuesday morning". Ava propose 3 créneaux, tu en prends un.
2. Le SMS arrive sur ton portable (montre-le à la caméra).
3. Tu rafraîchis le dashboard : le RDV est là, l'événement est dans Google Calendar.
4. Tu appelles le numéro Vapi depuis ton portable : "I need to move my appointment". Ava te reconnaît par ton numéro (depuis la page web, elle te demande le code reçu par SMS).
5. Tu demandes un créneau un dimanche → refus poli + alternatives réelles. Montre que ce n'est pas l'IA qui décide.

Cette vidéo + le lien `/demo` + le repo GitHub = ton premier projet de portfolio crédible.
