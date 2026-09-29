-- Seed data for the fixed category taxonomy. See runbooks/category-taxonomy.md.
-- Re-runnable: INSERT OR IGNORE so re-seeding an existing DB is a no-op.

-- Top-level
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('housing',             'Housing',                     NULL),
    ('auto',                'Auto',                         NULL),
    ('food',                'Food',                         NULL),
    ('insurance',           'Insurance',                    NULL),
    ('healthcare',          'Healthcare & Medical',         NULL),
    ('personal_shopping',   'Personal & Shopping',          NULL),
    ('entertainment',       'Entertainment & Subscriptions',NULL),
    ('travel',              'Travel',                       NULL),
    ('education',           'Education',                    NULL),
    ('childcare',           'Childcare',                    NULL),
    ('pets',                'Pets',                         NULL),
    ('gifts_charity',       'Gifts & Charity',               NULL),
    ('income',              'Income',                       NULL),
    ('transfers',           'Transfers',                    NULL),
    ('fees_interest',       'Fees & Interest',               NULL),
    ('taxes',               'Taxes',                        NULL),
    ('business',            'Business',                     NULL),
    ('hobbies',             'Hobbies',                      NULL),
    ('uncategorized',       'Uncategorized',                NULL);

-- Housing
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('housing_mortgage_rent',       'Mortgage/Rent Payment',    'housing'),
    ('housing_property_tax',        'Property Tax',             'housing'),
    ('housing_hoa',                 'HOA',                      'housing'),
    ('housing_utilities',           'Utilities',                'housing'),
    ('housing_insurance',           'Home Insurance',           'housing'),
    ('housing_repairs',             'Repairs & Maintenance',    'housing'),
    ('housing_management_fee',      'Property Management Fee',  'housing'),
    ('housing_leasing_advertising', 'Leasing & Advertising',    'housing'),
    ('housing_legal_professional',  'Legal & Professional',     'housing');

-- Auto
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('auto_fuel',           'Fuel',                  'auto'),
    ('auto_maintenance',    'Maintenance & Repairs', 'auto'),
    ('auto_insurance',      'Auto Insurance',        'auto'),
    ('auto_registration',   'Registration & DMV',    'auto'),
    ('auto_loan_payment',   'Auto Loan Payment',     'auto'),
    ('auto_parking_tolls',  'Parking & Tolls',       'auto');

-- Food
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('food_groceries', 'Groceries',            'food'),
    ('food_dining',    'Dining & Restaurants', 'food');

-- Insurance (not tied to a specific asset)
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('insurance_health',         'Health',         'insurance'),
    ('insurance_life',           'Life',           'insurance'),
    ('insurance_umbrella_other', 'Umbrella/Other', 'insurance');

-- Healthcare & Medical
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('healthcare_doctor_dental_vision', 'Doctor/Dental/Vision', 'healthcare'),
    ('healthcare_pharmacy',             'Pharmacy',             'healthcare');

-- Personal & Shopping
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('personal_clothing',        'Clothing',        'personal_shopping'),
    ('personal_electronics',     'Electronics',     'personal_shopping'),
    ('personal_household_goods', 'Household Goods', 'personal_shopping'),
    ('personal_care',            'Personal Care',   'personal_shopping');

-- Entertainment & Subscriptions
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('entertainment_subscriptions',  'Streaming/Subscriptions', 'entertainment'),
    ('entertainment_events_hobbies', 'Events/Hobbies',          'entertainment');

-- Travel
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('travel_flights',        'Flights',        'travel'),
    ('travel_lodging',        'Lodging',        'travel'),
    ('travel_transportation', 'Transportation', 'travel');

-- Education
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('education_tuition',       'Tuition',       'education'),
    ('education_supplies',      'Supplies',      'education'),
    ('education_student_loans', 'Student Loans', 'education');

-- Childcare
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('childcare_daycare_babysitting', 'Daycare/Babysitting', 'childcare'),
    ('childcare_activities',          'Activities',          'childcare');

-- Pets
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('pets_vet',            'Vet',              'pets'),
    ('pets_food_supplies',  'Food & Supplies',  'pets'),
    ('pets_grooming',       'Grooming',         'pets');

-- Gifts & Charity
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('gifts',                'Gifts',                'gifts_charity'),
    ('charitable_donations', 'Charitable Donations', 'gifts_charity');

-- Income
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('income_salary',              'Salary',               'income'),
    ('income_rental',               'Rental Income',        'income'),
    ('income_interest_dividends',   'Interest & Dividends', 'income'),
    ('income_reimbursement',        'Reimbursement',        'income'),
    ('income_other',                'Other Income',         'income');

-- Transfers (money moving between your own accounts — excluded from spending totals)
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('transfers_credit_card_payment',   'Credit Card Payment',  'transfers'),
    ('transfers_account_transfer',      'Account Transfer',     'transfers'),
    ('transfers_savings_contribution',  'Savings Contribution', 'transfers');

-- Fees & Interest
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('fees_bank',                  'Bank Fees',           'fees_interest'),
    ('fees_credit_card_interest',  'Credit Card Interest','fees_interest'),
    ('fees_loan_interest',         'Loan Interest',       'fees_interest');

-- Taxes (your income tax — property tax lives under Housing via entity_id)
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('taxes_income_tax',           'Income Tax',              'taxes'),
    ('taxes_estimated_payments',   'Estimated Tax Payments',  'taxes');

-- Business
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('business_supplies',            'Supplies',            'business'),
    ('business_software_services',   'Software/Services',   'business'),
    ('business_professional_fees',   'Professional Fees',   'business'),
    ('business_travel',              'Travel',              'business');

-- Hobbies. Ids follow the API's own {parent_id}_{slug} rule from
-- _unique_category_slug, so a category first created through
-- POST /categories and later seeded gets the same id either way.
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('hobbies_supplies',  'Supplies',  'hobbies'),
    ('hobbies_classes',   'Classes',   'hobbies');

-- Down payment on a home. A capital outflow tied to one property, so it
-- lives under Housing with entity_id naming the property, not under
-- Transfers -- the money left; it did not move to another of the owner's
-- accounts. Id from _unique_category_slug, because a live row may arrive
-- through POST /categories before this seed does.
INSERT OR IGNORE INTO categories (id, name, parent_id) VALUES
    ('housing_down_payment', 'Down Payment', 'housing');
