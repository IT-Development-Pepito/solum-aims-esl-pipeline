USE [ESL];


GO
SET ANSI_NULLS ON;


GO
SET QUOTED_IDENTIFIER ON;


GO
/* ============================================================
   TABLE: dbo.tb_ESL

   Current logical row identity:
       STORE_CODE + ITEM_CODE

   Current observed row count:
       26,512 rows

   Notes:
   - This DDL represents the CURRENT table structure.
   - Promotion-selection and BSP_PRICE_CATG findings belong
     to RefreshESL_New logic, not to this table DDL.
   ============================================================ */
CREATE TABLE [dbo].[tb_ESL] (
    [STORE_CODE]          VARCHAR (10)   NOT NULL,
    [ITEM_CODE]           VARCHAR (30)   NOT NULL,
    [BARCODE]             VARCHAR (50)   NULL,
    [ITEM_NAME]           VARCHAR (500)  NULL,
    [ITEM_SHORTNAME]      VARCHAR (500)  NULL,
    [SALES_PRICE]         INT            NULL,
    [DISC_PRICE]          INT            NULL,
    [DISC_PERCENT]        FLOAT          NULL,
    [DISC_TEXT]           VARCHAR (200)  NULL,
    [MEMBER_PRICE]        INT            NULL,
    [SOH]                 FLOAT          NULL,
    [EARLY_EXPIRY_DATE]   DATE           NULL,
    [PROD_WEIGHT]         INT            NULL,
    [MIN_QTY]             INT            NULL,
    [MAX_QTY]             INT            NULL,
    [PRODUCT_URL]         VARCHAR (1000) NULL,
    [DIVISION]            VARCHAR (100)  NULL,
    [DEPARTMENT]          VARCHAR (100)  NULL,
    [CLASS]               VARCHAR (200)  NULL,
    [SUBCLASS]            VARCHAR (200)  NULL,
    [BRAND]               VARCHAR (200)  NULL,
    [CLASS_ROTATION]      VARCHAR (20)   NULL,
    [NFC_URL]             VARCHAR (1000) NULL,
    [CONSIGMENT]          VARCHAR (5)    NULL,
    [RETURNABLE]          VARCHAR (5)    NULL,
    [EXPIRY_DAYS]         INT            NULL,
    [DISPLAY_QTY]         INT            NULL,
    [LAST_UPDATED_DATE]   DATETIME       NULL,
    [SYNC_REC]            INT            NULL,
    [UOM]                 VARCHAR (20)   NULL,
    [PROMO_FLAG]          VARCHAR (10)   NULL,
    [PER_GRM_PROMO_PRICE] FLOAT          NULL,
    [PER_GRM_SELL_PRICE]  FLOAT          NULL,
    [PROMOTION_TYPE]      VARCHAR (100)  NULL,
    [CAMPAIGN_GROUP]      VARCHAR (100)  NULL,
    [REDLIST]             VARCHAR (10)   NULL,
    [SAVE_AMT]            INT            NULL,
    [CREATED_DATE]        DATETIME       NOT NULL,
    [PROMO_START_DATE]    VARCHAR (8)    NULL,
    [PROMO_END_DATE]      VARCHAR (8)    NULL,
    [PROMO_START_TIME]    VARCHAR (8)    NULL,
    [PROMO_END_TIME]      VARCHAR (8)    NULL,
    CONSTRAINT [PK_tb_ESL] PRIMARY KEY CLUSTERED ([STORE_CODE] ASC, [ITEM_CODE] ASC) WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY];


GO
/* ============================================================
   DEFAULT
   ============================================================ */
ALTER TABLE [dbo].[tb_ESL]
    ADD DEFAULT (GETDATE()) FOR [CREATED_DATE];


GO
/* ============================================================
   NONCLUSTERED INDEXES
   Reconstructed from current live sys.indexes /
   sys.index_columns results.
   ============================================================ */
/* ------------------------------------------------------------
   Index 2
   BARCODE lookup / covering index
   ------------------------------------------------------------ */
CREATE NONCLUSTERED INDEX [IX_tb_ESL_BARCODE]
    ON [dbo].[tb_ESL]([BARCODE] ASC)
    INCLUDE([ITEM_NAME], [SALES_PRICE], [DISC_PRICE], [SOH]);


GO
/* ------------------------------------------------------------
   Index 3
   Synchronization-related index

   Current observed structure:
       KEY     : SYNC_REC
       INCLUDE : STORE_CODE
                 ITEM_CODE
                 LAST_UPDATED_DATE

   Current usage snapshot:
       UserSeeks   = 0
       UserScans   = 296
       UserUpdates = 231

   Keep as-is for current-condition DDL.
   Design should be reviewed separately.
   ------------------------------------------------------------ */
CREATE NONCLUSTERED INDEX [IX_tb_ESL_SYNC_REC]
    ON [dbo].[tb_ESL]([SYNC_REC] ASC)
    INCLUDE([STORE_CODE], [ITEM_CODE], [LAST_UPDATED_DATE]);


GO
/* ------------------------------------------------------------
   Index 4
   Promotion-related index

   Current observed structure:
       KEY:
           PROMOTION_TYPE
           CAMPAIGN_GROUP

       INCLUDE:
           ITEM_CODE
           SALES_PRICE
           DISC_PRICE

   Current usage snapshot:
       UserSeeks   = 0
       UserScans   = 3
       UserUpdates = 231

   This is the CURRENT definition, not yet redesigned for
   STORE_CODE + promotion predicates.
   ------------------------------------------------------------ */
CREATE NONCLUSTERED INDEX [IX_tb_ESL_PROMOTION]
    ON [dbo].[tb_ESL]([PROMOTION_TYPE] ASC, [CAMPAIGN_GROUP] ASC)
    INCLUDE([ITEM_CODE], [SALES_PRICE], [DISC_PRICE]);


GO
/* ------------------------------------------------------------
   Index 5
   LAST_UPDATED_DATE
   ------------------------------------------------------------ */
CREATE NONCLUSTERED INDEX [IX_tb_ESL_LAST_UPDATED]
    ON [dbo].[tb_ESL]([LAST_UPDATED_DATE] ASC);


GO
/* ------------------------------------------------------------
   Index 6
   REDLIST-related index
   ------------------------------------------------------------ */
CREATE NONCLUSTERED INDEX [IX_tb_ESL_REDLIST]
    ON [dbo].[tb_ESL]([REDLIST] ASC)
    INCLUDE([ITEM_CODE], [ITEM_NAME]);