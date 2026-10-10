*Alice never sees Bob's laptop.*

# BUILD_LOG

> Copy this file to `BUILD_LOG.md`. Fill in each stage **as you go**, not at the end: the
> decision **before** you prompt your agent, the prediction **before** you run the check.
>
> This is your record of what *you* understood. A person reads it for `G-DESIGN` and
> `G-ENFORCE`. Your coding agent is instructed not to write it for you; if the words aren't
> yours, it shows.
>
> Three lines per part is plenty. "I expected X, got Y, because Z" beats a paragraph.

---

## Stage 1: the database
- **I decided:** A reset script that drops and recreates tables. It is the simpler and faster option. 
- **I predicted:** Running the seed twice without reset will result in dirty data and errors. Depending on unique keys. E.g. users will fail as email is unique while Orders will be inserted twice.
- **What happened (paste output):**
`"psql -d online_shop -c "select count(*) from users;"` gives 10.\
`psql -d online_shop -c "select order_id, customer_email, status from customer_orders order by order_id;"` matches EVAL 2' table.\
`psql -d online_shop -c "insert into actions_log (user_email, action_type, parameters) values ('x','RETURN_ITEM','{}');"` gives an error\
`ERROR:  new row for relation "actions_log" violates check constraint "actions_log_action_type_check"\
DETAIL:  Failing row contains (1, 2026-10-09 23:36:14.382901+11, x, RETURN_ITEM, {}).`\
Seeding twice didn't break anything as a reset was done.\
Without reset, seeding twice gave what was expected:\
`ERROR:  duplicate key value violates unique constraint "users_email_key"\
DETAIL:  Key (email)=(alice.jones@example.com) already exists.`\
AND\
`psql -d online_shop -c "select count(*) from customer_orders;"` gave 18 rows. Order for `'alice.jones@example.com'` was inserted again.

## Stage 2: the tools, and where access control lives
- **Before reading SPEC R-1, I thought ownership belonged in:** In the tool. As putting it only in the prompt is not guaranteed block from unauthorised access. Model behavior is not deterministic. Anything that needs a fixed outcome like one user not being able to access another user's order should be in the tool.\
- **I decided:** To keep it in the tool. Did not change my mind after reading the SPEC.\
- **What happened (order 5 as alice):** Order 3 as Alice returned okay.\
`HTTP/1.1 200 OK
Content-Type: application/json
Vary: Origin
Date: Fri, 09 Oct 2026 13:22:59 GMT
Content-Length: 304`

`{"result":"[{\"order_id\":3,\"status\":\"SHIPPED\",\"order_date\":\"2026-10-07T23:46:18.365919+11:00\",\"total_amount\":120.00,\"items\":[{\"price\":120,\"product\":\"Mechanical Keyboard\",\"qty\":1}],\"delivery_address\":\"123 Market St, Springfield\",\"customer_email\":\"alice.jones@example.com\"}]"}`
\

Order 5 as Alice did not return Bob's orde. {"result"} is empty\
`HTTP/1.1 200 OK
Content-Type: application/json
Vary: Origin
Date: Fri, 09 Oct 2026 13:25:19 GMT
Content-Length: 16`

`{"result":"[]"}`\

`{"result":"[{\"order_id\":3, ...}]"}` data is JSON inside JSON.

## Stage 3: the agent, and a bare CLI
- **I decided (tools loaded, how the email is bound):** The agent gets all three tools, and the email is bound with fixed value at login. Each user gets their own toolset with their email filled in as it will be simpler and harder to get wrong. Sharing the same toolset for all users by calling a function each time to find the email, for example by looking at the current session, can cause data leak. It is more flexible, but if it reads the wrong session, it uses the wrong user’s email. This covers both parameter names - customer_email and user_email
**Note:** Toolbox runs on port 5001 because macOS AirPlay Receiver uses 5000
- **I predicted the "order 5 for bob" request would:** not give information about Bob's order 
- **What happened:** It did not give information about Bob's order. It said "I can't find that order on your account".

## Stage 4: the pipeline and its events
- **My hand-sketched CLI lines:**
- **I predicted the first event after the agent stage would be:**
- **What happened:**

## Stage 5: one trace per turn
- **I decided (what goes in span attributes, who can see Phoenix):**
- **Trace id:**
- **One thing the trace showed that the reply didn't:**

## Stage 6: Sanitizer and Security Judge
- **I decided (who may block, which A2A method):**
- **Predicted vs actual X01 latency:**
- **When I stopped the Judge, my pipeline first:**
- **Failing trajectory saved at:**

## Stage 7: the Guardrail
- **Three messages that must pass / three that must not (written before the prompt):**
- **False blocks / off-topic blocks, per prompt version:**

  | Version | What I changed | Legit false blocks | Off-topic blocked |
  |---|---|---|---|
  | v1 | | | |

## Stage 8: Masker and memory
- **I decided (what counts as PII, the cutoff):**
- **My planted memory's score, and whether my cutoff kept it:**
- **What the Masker reported on my PII test:**

## Stage 9: the web UI
- **My sketch, in words:**
- **Something the UI shows that the CLI doesn't (feature or leak?):**

## Stage 10: the eval runner
- **How I handled the memory waits:**
- **First run's failing rows, and what I changed:**
- **Second run: see `reports/eval.json` (don't retype numbers here).**
- **Successful turn I read end to end (trace id), and what it taught me:**
- **Failing turn I read end to end (trace id), and what it taught me:**
