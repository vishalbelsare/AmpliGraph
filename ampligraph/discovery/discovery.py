import logging
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.neighbors import NearestNeighbors
from scipy import optimize, spatial
import networkx as nx

from ..evaluation import evaluate_performance, filter_unseen_entities

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def discover_facts(X, model, top_n=10, strategy='random_uniform', max_candidates=.3, target_rel=None, seed=0):
    """
    Discover new facts from an existing knowledge graph.

    You should use this function when you already have a model trained on a knowledge graph and you want to
    discover potentially true statements in that knowledge graph.

    The general procedure of this function is to generate a set of candidate statements :math:`C` according to some
    sampling strategy ``strategy``, then rank them against a set of corruptions using the
    :meth:`ampligraph.evaluation.evaluate_performance` function.
    Candidates that appear in the ``top_n`` ranked statements of this procedure are returned as likely true
    statements.

    The majority of the strategies are implemented with the same underlying principle of searching for
    candidate statements:

    - from among the less frequent entities ('entity_frequency'),
    - less connected entities ('graph_degree', cluster_coefficient'),
    - | less frequent local graph structures ('cluster_triangles', 'cluster_squares'), on the assumption that densely
        connected entities are less likely to have missing true statements.
    - | The remaining strategies ('random_uniform', 'exhaustive') generate candidate statements by a random sampling
        of entity and relations and exhaustively, respectively.

    .. warning::
        Due to the significant amount of computation required to evaluate all triples using the 'exhaustive' strategy,
        we do not recommend its use at this time.

    The function will automatically filter entities that haven't been seen by the model, and operates on
    the assumption that the model provided has been fit on the data ``X`` (determined heuristically), although ``X``
    may be a subset of the original data, in which case a warning is shown.

    The ``target_rel`` argument indicates what relation to generate candidate statements for. If this is set to ``None``
    then all target relations will be considered for sampling.

    Parameters
    ----------

    X : ndarray, shape [n, 3]
        The input knowledge graph used to train ``model``, or a subset of it.
    model : EmbeddingModel
        The trained model that will be used to score candidate facts.
    top_n : int
        The cutoff position in ranking to consider a candidate triple as true positive.
    strategy: string
        The candidates generation strategy:

        - | 'exhaustive' : generates all possible candidates given the ```target_rel```
            and ```consolidate_sides``` parameter.
        - | 'random_uniform' : generates N candidates (N <= max_candidates) based on a uniform random
            sampling of head and tail entities.
        - 'entity_frequency' : generates candidates by sampling entities with low frequency.
        - 'graph_degree' : generates candidates by sampling entities with a low graph degree.
        - 'cluster_coefficient' : generates candidates by sampling entities with a low clustering coefficient.
        - 'cluster_triangles' : generates candidates by sampling entities with a low number of cluster triangles.
        - 'cluster_squares' : generates candidates by sampling entities with a low number of cluster squares.

    max_candidates: int or float
        The maximum numbers of candidates generated by 'strategy'.
        Can be an absolute number or a percentage [0,1].
    target_rel : str
        Target relation to focus on. The function will discover facts only for that specific relation type.
        If None, the function attempts to discover new facts for all relation types in the graph.
    seed : int
        Seed to use for reproducible results.


    Returns
    -------
    X_pred : ndarray, shape [n, 3]
        A list of new facts predicted to be true.


    Examples
    --------
    >>> import requests
    >>> from ampligraph.datasets import load_from_csv
    >>> from ampligraph.latent_features import ComplEx
    >>> from ampligraph.discovery import discover_facts
    >>>
    >>> # Game of Thrones relations dataset
    >>> url = 'https://ampligraph.s3-eu-west-1.amazonaws.com/datasets/GoT.csv'
    >>> open('GoT.csv', 'wb').write(requests.get(url).content)
    >>> X = load_from_csv('.', 'GoT.csv', sep=',')
    >>>
    >>> model = ComplEx(batches_count=10, seed=0, epochs=200, k=150, eta=5,
    >>>                 optimizer='adam', optimizer_params={'lr':1e-3},
    >>>                 loss='multiclass_nll', regularizer='LP',
    >>>                 regularizer_params={'p':3, 'lambda':1e-5},
    >>>                 verbose=True)
    >>> model.fit(X)
    >>>
    >>> discover_facts(X, model, top_n=3, max_candidates=20000, strategy='entity_frequency',
    >>>                target_rel='ALLIED_WITH', seed=42)
    array([['House Reed of Greywater Watch', 'ALLIED_WITH', 'Sybelle Glover'],
           ['Hugo Wull', 'ALLIED_WITH', 'House Norrey'],
           ['House Grell', 'ALLIED_WITH', 'Delonne Allyrion'],
           ['Lorent Lorch', 'ALLIED_WITH', 'House Ruttiger']], dtype=object)


    """

    if not model.is_fitted:
        msg = 'Model is not fitted.'
        logger.error(msg)
        raise ValueError(msg)

    if not model.is_fitted_on(X):
        msg = 'Model might not be fitted on this data.'
        logger.warning(msg)
        # raise ValueError(msg)

    if strategy not in ['exhaustive', 'random_uniform', 'entity_frequency', 'graph_degree', 'cluster_coefficient',
                        'cluster_triangles', 'cluster_squares']:
        msg = '%s is not a valid strategy.' % strategy
        logger.error(msg)
        raise ValueError(msg)

    if target_rel is None:
        msg = 'No target relation specified. Using all relations to ' \
              'generate candidate statements.'
        logger.info(msg)
    else:
        if target_rel not in model.rel_to_idx.keys():
            msg = 'Target relation not found in model.'
            logger.error(msg)
            raise ValueError(msg)

    # Set random seed
    np.random.seed(seed)

    # Remove unseen entities
    X_filtered = filter_unseen_entities(X, model)

    if target_rel is None:
        rel_list = [x for x in model.rel_to_idx.keys()]
    else:
        rel_list = [target_rel]

    discoveries = []

    # Iterate through relations
    for relation in rel_list:

        logger.debug('Generating candidates for relation: %s' % relation)
        candidate_generator = generate_candidates(X_filtered, strategy,
                                                  relation, max_candidates,
                                                  seed=seed)

        for candidates in candidate_generator:

            logger.debug('Generated %d candidate statements.' % len(candidates))

            # Get ranks of candidate statements
            X = candidates
            ranks = evaluate_performance(X, model=model, filter_triples=X, use_default_protocol=True, verbose=True)

            # Select candidate statements within the top_n predicted ranks standard protocol evaluates against
            # corruptions on both sides, we just average the ranks here
            avg_ranks = np.mean(ranks, axis=1)

            preds = np.array(avg_ranks) <= top_n
            discoveries.append(candidates[preds])

    logger.info('Discovered %d facts' % len(discoveries))

    return np.hstack(discoveries)


def generate_candidates(X, strategy, target_rel, max_candidates, consolidate_sides=False, seed=0):
    """ Generate candidate statements from an existing knowledge graph using a defined strategy.

        Parameters
        ----------

        strategy: string
            The candidates generation strategy.
            - 'exhaustive' : generates all possible candidates given the ```target_rel``` and
                ```consolidate_sides``` parameter.
            - 'random_uniform' : generates N candidates (N <= max_candidates) based on a uniform random sampling of
                head and tail entities.
            - 'entity_frequency' : generates candidates by sampling entities with low frequency.
            - 'graph_degree' : generates candidates by sampling entities with a low graph degree.
            - 'cluster_coefficient' : generates candidates by sampling entities with a low clustering coefficient.
            - 'cluster_triangles' : generates candidates by sampling entities with a low number of cluster triangles.
            - 'cluster_squares' : generates candidates by sampling entities with a low number of cluster squares.
        max_candidates: int or float
            The maximum numbers of candidates generated by 'strategy'.
            Can be an absolute number or a percentage [0,1].
            This does not guarantee the number of candidates generated.
        target_rel : str
            Target relation to focus on. The function will generate candidate
             statements only with this specific relation type.
        consolidate_sides: bool
            If True will generate candidate statements as a product of
            unique head and tail entities, otherwise will
            consider head and tail entities separately. Default: False.
        seed : int
            Seed to use for reproducible results.

        Returns
        -------
        X_candidates : ndarray, shape [n, 3]
            A list of candidate statements.


        Examples
        --------
        >>> import numpy as np
        >>> from ampligraph.discovery.discovery import generate_candidates
        >>>
        >>> X = np.array([['a', 'y', 'b'],
        >>>               ['b', 'y', 'a'],
        >>>               ['a', 'y', 'c'],
        >>>               ['c', 'y', 'a'],
        >>>               ['a', 'y', 'd'],
        >>>               ['c', 'y', 'd'],
        >>>               ['b', 'y', 'c'],
        >>>               ['f', 'y', 'e']])

        >>> X_candidates = generate_candidates(X, strategy='graph_degree',
        >>>                                     target_rel='y', max_candidates=3)
        >>> ([['a', 'y', 'e'],
        >>>  ['f', 'y', 'a'],
        >>>  ['c', 'y', 'e']])

    """

    if strategy not in ['random_uniform', 'exhaustive', 'entity_frequency',
                        'graph_degree', 'cluster_coefficient',
                        'cluster_triangles', 'cluster_squares']:
        msg = '%s is not a valid candidate generation strategy.' % strategy
        raise ValueError(msg)

    if target_rel not in np.unique(X[:, 1]):
        # No error as may be case where target_rel is not in X
        msg = 'Target relation is not found in triples.'
        logger.warning(msg)

    if not isinstance(max_candidates, (float, int)):
        msg = 'Parameter max_candidates must be a float or int.'
        raise ValueError(msg)

    if max_candidates <= 0:
        msg = 'Parameter max_candidates must be a positive integer ' \
              'or float in range (0,1].'
        raise ValueError(msg)

    if isinstance(max_candidates, float):
        max_candidates = int(max_candidates * len(X))

    # Set random seed
    np.random.seed(seed)

    # Get entities linked with this relation
    if consolidate_sides:
        e_s = np.unique(np.concatenate((X[:, 0], X[:, 2])))
        e_o = e_s
    else:
        e_s = np.unique(X[:, 0])
        e_o = np.unique(X[:, 2])

    logger.info('Generating candidates using {} strategy.'.format(strategy))

    def _filter_candidates(X_candidates, X):

        # Filter statements that are in X
        X_candidates = _setdiff2d(X_candidates, X)

        # Filter statements that are ['x', rel, 'x']
        keep_idx = np.where(X_candidates[:, 0] != X_candidates[:, 2])
        return X_candidates[keep_idx]

    if strategy == 'exhaustive':

        # Exhaustive, generate all combinations of subject and object
        # entities for target_rel

        # Generate all combinates for a single entity at each iteration
        for ent in e_s:
            X_candidates = np.array(np.meshgrid(ent, target_rel, e_o)).T.reshape(-1, 3)

            X_candidates = _filter_candidates(X_candidates, X)

            yield X_candidates

    elif strategy == 'random_uniform':

        # Take close to sqrt of max_candidates so that:
        #   len(meshgrid result) == max_candidates
        sample_size = int(np.sqrt(max_candidates))

        sample_e_s = np.random.choice(e_s, size=sample_size, replace=False)
        sample_e_o = np.random.choice(e_o, size=sample_size, replace=False)

        X_candidates = np.array(np.meshgrid(sample_e_s, target_rel, sample_e_o)).T.reshape(-1, 3)

        X_candidates = _filter_candidates(X_candidates, X)

        yield X_candidates

    elif strategy == 'entity_frequency':

        # Get entity counts and sort them in ascending order
        ent_counts = np.array(np.unique(X[:, [0, 2]], return_counts=True)).T
        ent_counts = ent_counts[ent_counts[:, 1].argsort()]

        sample_size = int(np.sqrt(max_candidates))

        sample_e_s = np.random.choice(ent_counts[0:max_candidates, 0], size=sample_size, replace=False)
        sample_e_o = np.random.choice(ent_counts[0:max_candidates, 0], size=sample_size, replace=False)

        X_candidates = np.array(np.meshgrid(sample_e_s, target_rel, sample_e_o)).T.reshape(-1, 3)
        X_candidates = _filter_candidates(X_candidates, X)

        yield X_candidates

    elif strategy in ['graph_degree', 'cluster_coefficient',
                      'cluster_triangles', 'cluster_squares']:

        # Create networkx graph
        G = nx.Graph()
        for row in X:
            G.add_nodes_from([row[0], row[2]])
            G.add_edge(row[0], row[2], name=row[1])

        # Calculate node metrics
        if strategy == 'graph_degree':
            C = {i: j for i, j in G.degree()}
        elif strategy == 'cluster_coefficient':
            C = nx.algorithms.cluster.clustering(G)
        elif strategy == 'cluster_triangles':
            C = nx.algorithms.cluster.triangles(G)
        elif strategy == 'cluster_squares':
            C = nx.algorithms.cluster.square_clustering(G)

        # Convert to np.array and sort metric column in descending order
        C = np.array([[k, v] for k, v in C.items()])
        C = C[C[:, 1].argsort()]

        sample_size = int(np.sqrt(max_candidates))

        sample_e_s = np.random.choice(C[0:max_candidates, 0], size=sample_size, replace=False)
        sample_e_o = np.random.choice(C[0:max_candidates, 0], size=sample_size, replace=False)

        X_candidates = np.array(np.meshgrid(sample_e_s, target_rel, sample_e_o)).T.reshape(-1, 3)
        X_candidates = _filter_candidates(X_candidates, X)

        yield X_candidates

    return


def _setdiff2d(A, B):
    """ Utility function equivalent to numpy.setdiff1d on 2d arrays.

    Parameters
    ----------

    A : ndarray, shape [n, m]

    B : ndarray, shape [n, m]

    Returns
    -------
    np.array, shape [k, m]
        Rows of A that are not in B.

    """

    if len(A.shape) != 2 or len(B.shape) != 2:
        raise RuntimeError('Input arrays must be 2-dimensional.')

    tmp = np.prod(np.swapaxes(A[:, :, None], 1, 2) == B, axis=2)
    return A[~ np.sum(np.cumsum(tmp, axis=0) * tmp == 1, axis=1).astype(bool)]


def find_clusters(X, model, clustering_algorithm=DBSCAN(), mode="entity"):
    """
    Perform link-based cluster analysis on a knowledge graph.

    The clustering happens on the embedding space of the entities and relations.
    For example, if we cluster some entities of a model that uses `k=100` (i.e. embedding space of size 100),
    we will apply the chosen clustering algorithm on the 100-dimensional space of the provided input samples.

    Clustering can be used to evaluate the quality of the knowledge embeddings, by comparing to natural clusters.
    For example, in the example below we cluster the embeddings of international football matches and end up
    finding geographical clusters very similar to the continents.
    This comparison can be subjective by inspecting a 2D projection of the embedding space or objective using a
    `clustering metric <https://scikit-learn.org/stable/modules/clustering.html#clustering-performance-evaluation>`_.

    | The choice of the clustering algorithm and its corresponding tuning will greatly impact the results.
      Please see `scikit-learn documentation <https://scikit-learn.org/stable/modules/clustering.html#clustering>`_
      for a list of algorithms, their parameters, and pros and cons.

    Clustering is exclusive (i.e. a triple is assigned to one and only one cluster).

    Parameters
    ----------

    X : ndarray, shape [n, 3] or [n]
        The input to be clustered.
        ``X`` can either be the triples of a knowledge graph, its entities, or its relations.
        The argument ``mode`` defines whether ``X`` is supposed an array of triples
        or an array of either entities or relations.
    model : EmbeddingModel
        The fitted model that will be used to generate the embeddings.
        This model must have been fully trained already, be it directly with
        ``fit()`` or from a helper function such as :meth:`ampligraph.evaluation.select_best_model_ranking`.
    clustering_algorithm : object
        The initialized object of the clustering algorithm.
        It should be ready to apply the `fit_predict` method.
        Please see: `scikit-learn documentation <https://scikit-learn.org/stable/modules/clustering.html#clustering>`_
        to understand the clustering API provided by scikit-learn.
        The default clustering model is
        `sklearn's DBSCAN <https://scikit-learn.org/stable/modules/generated/sklearn.cluster.DBSCAN.html>`_
        with its default parameters.
    mode: string
        Clustering mode. Choose from:

        - | 'entity' (default): the algorithm will cluster the embeddings of the provided entities.
        - | 'relation': the algorithm will cluster the embeddings of the provided relations.
        - | 'triple' : the algorithm will cluster the concatenation
            of the embeddings of the subject, predicate and object for each triple.

    Returns
    -------
    labels : ndarray, shape [n]
        Index of the cluster each triple belongs to.

    Examples
    --------
    >>> # Note seaborn, matplotlib, adjustText are not AmpliGraph dependencies.
    >>> # and must therefore be installed manually as:
    >>> #
    >>> # $ pip install seaborn matplotlib adjustText
    >>>
    >>> import requests
    >>> import pandas as pd
    >>> import numpy as np
    >>> from sklearn.decomposition import PCA
    >>> from sklearn.cluster import KMeans
    >>> import matplotlib.pyplot as plt
    >>> import seaborn as sns
    >>>
    >>> # adjustText lib: https://github.com/Phlya/adjustText
    >>> from adjustText import adjust_text
    >>>
    >>> from ampligraph.datasets import load_from_csv
    >>> from ampligraph.latent_features import ComplEx
    >>> from ampligraph.discovery import find_clusters
    >>>
    >>> # International football matches triples
    >>> # See tutorial here to understand how the triples are created from a tabular dataset:
    >>> # https://github.com/Accenture/AmpliGraph/blob/master/docs/tutorials/\
ClusteringAndClassificationWithEmbeddings.ipynb
    >>> url = 'https://ampligraph.s3-eu-west-1.amazonaws.com/datasets/football.csv'
    >>> open('football.csv', 'wb').write(requests.get(url).content)
    >>> X = load_from_csv('.', 'football.csv', sep=',')[:, 1:]
    >>>
    >>> model = ComplEx(batches_count=50,
    >>>                 epochs=300,
    >>>                 k=100,
    >>>                 eta=20,
    >>>                 optimizer='adam',
    >>>                 optimizer_params={'lr':1e-4},
    >>>                 loss='multiclass_nll',
    >>>                 regularizer='LP',
    >>>                 regularizer_params={'p':3, 'lambda':1e-5},
    >>>                 seed=0,
    >>>                 verbose=True)
    >>> model.fit(X)
    >>>
    >>> df = pd.DataFrame(X, columns=["s", "p", "o"])
    >>>
    >>> teams = np.unique(np.concatenate((df.s[df.s.str.startswith("Team")],
    >>>                                   df.o[df.o.str.startswith("Team")])))
    >>> team_embeddings = model.get_embeddings(teams, embedding_type='entity')
    >>>
    >>> embeddings_2d = PCA(n_components=2).fit_transform(np.array([i for i in team_embeddings]))
    >>>
    >>> # Find clusters of embeddings using KMeans
    >>> kmeans = KMeans(n_clusters=6, n_init=100, max_iter=500)
    >>> clusters = find_clusters(teams, model, kmeans, mode='entity')
    >>>
    >>> # Plot results
    >>> df = pd.DataFrame({"teams": teams, "clusters": "cluster" + pd.Series(clusters).astype(str),
    >>>                    "embedding1": embeddings_2d[:, 0], "embedding2": embeddings_2d[:, 1]})
    >>>
    >>> plt.figure(figsize=(10, 10))
    >>> plt.title("Cluster embeddings")
    >>>
    >>> ax = sns.scatterplot(data=df, x="embedding1", y="embedding2", hue="clusters")
    >>>
    >>> texts = []
    >>> for i, point in df.iterrows():
    >>>     if np.random.uniform() < 0.1:
    >>>         texts.append(plt.text(point['embedding1']+.02, point['embedding2'], str(point['teams'])))
    >>> adjust_text(texts)

    .. image:: ../../docs/img/clustering/clustered_embeddings_docstring.png

    """
    if not model.is_fitted:
        msg = "Model has not been fitted."
        logger.error(msg)
        raise ValueError(msg)

    if not hasattr(clustering_algorithm, "fit_predict"):
        msg = "Clustering algorithm does not have the `fit_predict` method."
        logger.error(msg)
        raise ValueError(msg)

    modes = ("triple", "entity", "relation")
    if mode not in modes:
        msg = "Argument `mode` must be one of the following: {}.".format(", ".join(modes))
        logger.error(msg)
        raise ValueError(msg)

    if mode == "triple" and (len(X.shape) != 2 or X.shape[1] != 3):
        msg = "For 'triple' mode the input X must be a matrix with three columns."
        logger.error(msg)
        raise ValueError(msg)

    if mode in ("entity", "relation") and len(X.shape) != 1:
        msg = "For 'entity' or 'relation' mode the input X must be an array."
        raise ValueError(msg)

    if mode == "triple":
        s = model.get_embeddings(X[:, 0], embedding_type='entity')
        p = model.get_embeddings(X[:, 1], embedding_type='relation')
        o = model.get_embeddings(X[:, 2], embedding_type='entity')
        emb = np.hstack((s, p, o))
    else:
        emb = model.get_embeddings(X, embedding_type=mode)

    return clustering_algorithm.fit_predict(emb)


def find_duplicates(X, model, mode="entity", metric='l2', tolerance='auto',
                    expected_fraction_duplicates=0.1, verbose=False):
    r"""
    Find duplicate entities, relations or triples in a graph based on their embeddings.

    For example, say you have a movie dataset that was scraped off the web with possible duplicate movies.
    The movies in this case are the entities.
    Therefore, you would use the 'entity' mode to find all the movies that could de duplicates of each other.

    Duplicates are defined as points whose distance in the embedding space are smaller than
    some given threshold (called the tolerance).

    The tolerance can be defined a priori or be found via an optimisation procedure given
    an expected fraction of duplicates. The optimisation algorithm applies a root-finding routine
    to find the tolerance that gets to the closest expected fraction. The routine always converges.

    Distance is defined by the chosen metric, which by default is the Euclidean distance (L2 norm).

    As the distances are calculated on the embedding space,
    the embeddings must be meaningful for this routine to work properly.
    Therefore, it is suggested to evaluate the embeddings first using a metric such as MRR
    before considering applying this method.

    Parameters
    ----------

    X : ndarray, shape [n, 3] or [n]
        The input to be clustered.
        X can either be the triples of a knowledge graph, its entities, or its relations.
        The argument `mode` defines whether X is supposed an array of triples
        or an array of either entities or relations.
    model : EmbeddingModel
        The fitted model that will be used to generate the embeddings.
        This model must have been fully trained already, be it directly with ``fit()``
        or from a helper function such as :meth:`ampligraph.evaluation.select_best_model_ranking`.
    mode: string
        Choose from:

        - | 'entity' (default): the algorithm will find duplicates of the provided entities based on their embeddings.
        - | 'relation': the algorithm will find duplicates of the provided relations based on their embeddings.
        - | 'triple' : the algorithm will find duplicates of the concatenation
            of the embeddings of the subject, predicate and object for each provided triple.

    metric: str
        A distance metric used to compare entity distance in the embedding space.
        `See options here <https://scikit-learn.org/stable/modules/generated/sklearn.neighbors.NearestNeighbors.html>`_.
    tolerance: int or str
        Minimum distance (depending on the chosen ``metric``) to define one entity as the duplicate of another.
        If 'auto', it will be determined automatically in a way that you get the ``expected_fraction_duplicates``.
        The 'auto' option can be much slower than the regular one, as the finding duplicate internal procedure
        will be repeated multiple times.
    expected_fraction_duplicates: float
        Expected fraction of duplicates to be found. It is used only when ``tolerance`` is 'auto'.
        Should be between 0 and 1 (default: 0.1).
    verbose: bool
        Whether to print evaluation messages during optimisation (if ``tolerance`` is 'auto'). Default: False.

    Returns
    -------
    duplicates : set of frozensets
        Each entry in the duplicates set is a frozenset containing all entities that were found to be duplicates
        according to the metric and tolerance.
        Each frozenset will contain at least two entities.

    tolerance: float
        Tolerance used to find the duplicates (useful in the case of the automatic tolerance option).

    Examples
    --------
    >>> import pandas as pd
    >>> import numpy as np
    >>> import re
    >>>
    >>> # The IMDB dataset used here is part of the Movies5 dataset found on:
    >>> # The Magellan Data Repository (https://sites.google.com/site/anhaidgroup/projects/data)
    >>> import requests
    >>> url = 'http://pages.cs.wisc.edu/~anhai/data/784_data/movies5.tar.gz'
    >>> open('movies5.tar.gz', 'wb').write(requests.get(url).content)
    >>> import tarfile
    >>> tar = tarfile.open('movies5.tar.gz', "r:gz")
    >>> tar.extractall()
    >>> tar.close()
    >>>
    >>> # Reading tabular dataset of IMDB movies and filling the missing values
    >>> imdb = pd.read_csv("movies5/csv_files/imdb.csv")
    >>> imdb["directors"] = imdb["directors"].fillna("UnknownDirector")
    >>> imdb["actors"] = imdb["actors"].fillna("UnknownActor")
    >>> imdb["genre"] = imdb["genre"].fillna("UnknownGenre")
    >>> imdb["duration"] = imdb["duration"].fillna("0")
    >>>
    >>> # Creating knowledge graph triples from tabular dataset
    >>> imdb_triples = []
    >>>
    >>> for _, row in imdb.iterrows():
    >>>     movie_id = "ID" + str(row["id"])
    >>>     directors = row["directors"].split(",")
    >>>     actors = row["actors"].split(",")
    >>>     genres = row["genre"].split(",")
    >>>     duration = "Duration" + str(int(re.sub("\D", "", row["duration"])) // 30)
    >>>
    >>>     directors_triples = [(movie_id, "hasDirector", d) for d in directors]
    >>>     actors_triples = [(movie_id, "hasActor", a) for a in actors]
    >>>     genres_triples = [(movie_id, "hasGenre", g) for g in genres]
    >>>     duration_triple = (movie_id, "hasDuration", duration)
    >>>
    >>>     imdb_triples.extend(directors_triples)
    >>>     imdb_triples.extend(actors_triples)
    >>>     imdb_triples.extend(genres_triples)
    >>>     imdb_triples.append(duration_triple)
    >>>
    >>> # Training knowledge graph embedding with ComplEx model
    >>> from ampligraph.latent_features import ComplEx
    >>>
    >>> model = ComplEx(batches_count=10,
    >>>                 seed=0,
    >>>                 epochs=200,
    >>>                 k=150,
    >>>                 eta=5,
    >>>                 optimizer='adam',
    >>>                 optimizer_params={'lr':1e-3},
    >>>                 loss='multiclass_nll',
    >>>                 regularizer='LP',
    >>>                 regularizer_params={'p':3, 'lambda':1e-5},
    >>>                 verbose=True)
    >>>
    >>> imdb_triples = np.array(imdb_triples)
    >>> model.fit(imdb_triples)
    >>>
    >>> # Finding duplicates movies (entities)
    >>> from ampligraph.discovery import find_duplicates
    >>>
    >>> entities = np.unique(imdb_triples[:, 0])
    >>> dups, _ = find_duplicates(entities, model, mode='entity', tolerance=0.4)
    >>> print(list(dups)[:3])
    [frozenset({'ID4048', 'ID4049'}), frozenset({'ID5994', 'ID5993'}), frozenset({'ID6447', 'ID6448'})]
    >>> print(imdb[imdb.id.isin((4048, 4049, 5994, 5993, 6447, 6448))][['movie_name', 'year']])
                        movie_name  year
    4048          Ulterior Motives  1993
    4049          Ulterior Motives  1993
    5993          Chinese Hercules  1973
    5994          Chinese Hercules  1973
    6447  The Stranglers of Bombay  1959
    6448  The Stranglers of Bombay  1959

    """

    if not model.is_fitted:
        msg = "Model has not been fitted."
        logger.error(msg)
        raise ValueError(msg)

    modes = ("triple", "entity", "relation")
    if mode not in modes:
        msg = "Argument `mode` must be one of the following: {}.".format(", ".join(modes))
        logger.error(msg)
        raise ValueError(msg)

    if mode == "triple" and (len(X.shape) != 2 or X.shape[1] != 3):
        msg = "For 'triple' mode the input X must be a matrix with three columns."
        logger.error(msg)
        raise ValueError(msg)

    if mode in ("entity", "relation") and len(X.shape) != 1:
        msg = "For 'entity' or 'relation' mode the input X must be an array."
        logger.error(msg)
        raise ValueError(msg)

    if mode == "triple":
        s = model.get_embeddings(X[:, 0], embedding_type='entity')
        p = model.get_embeddings(X[:, 1], embedding_type='relation')
        o = model.get_embeddings(X[:, 2], embedding_type='entity')
        emb = np.hstack((s, p, o))
    else:
        emb = model.get_embeddings(X, embedding_type=mode)

    def get_dups(tol):
        """
         Given tolerance, finds duplicate entities in a graph based on their embeddings.

         Parameters
         ----------
         tol: float
             Minimum distance (depending on the chosen metric) to define one entity as the duplicate of another.

         Returns
         -------
         duplicates : set of frozensets
             Each entry in the duplicates set is a frozenset containing all entities that were found to be duplicates
             according to the metric and tolerance.
             Each frozenset will contain at least two entities.

        """
        nn = NearestNeighbors(metric=metric, radius=tol)
        nn.fit(emb)
        neighbors = nn.radius_neighbors(emb)[1]
        idx_dups = ((i, row) for i, row in enumerate(neighbors) if len(row) > 1)
        if mode == "triple":
            dups = {frozenset(tuple(X[idx]) for idx in row) for i, row in idx_dups}
        else:
            dups = {frozenset(X[idx] for idx in row) for i, row in idx_dups}
        return dups

    def opt(tol, info):
        """
        Auxiliary function for the optimization procedure to find the tolerance that corresponds to the expected
        number of duplicates.

        Returns the difference between actual and expected fraction of duplicates.
        """
        duplicates = get_dups(tol)
        fraction_duplicates = len(set().union(*duplicates)) / len(emb)
        if verbose:
            info['Nfeval'] += 1
            logger.info("Eval {}: tol: {}, duplicate fraction: {}".format(info['Nfeval'], tol, fraction_duplicates))
        return fraction_duplicates - expected_fraction_duplicates

    if tolerance == 'auto':
        max_distance = spatial.distance_matrix(emb, emb).max()
        tolerance = optimize.bisect(opt, 0.0, max_distance, xtol=1e-3, maxiter=50, args=({'Nfeval': 0}, ))

    return get_dups(tolerance), tolerance


def query_topn(model, top_n=10, head=None, relation=None, tail=None, ents_to_consider=None, rels_to_consider=None):
    """Queries the model with two elements of a triple and returns the top_n results of
    all possible completions ordered by score predicted by the model.

    For example, given a <subject, predicate> pair in the arguments, the model will score
    all possible triples <subject, predicate, ?>, filling in the missing element with known
    entities, and return the top_n triples ordered by score. If given a <subject, object>
    pair it will fill in the missing element with known relations.

    .. note::
        This function does not filter out true statements - triples returned can include those
        the model was trained on.

    Parameters
    ----------
    model : EmbeddingModel
        The trained model that will be used to score triple completions.
    top_n : int
        The number of completed triples to returned.
    head : string
        An entity string to query.
    relation : string
        A relation string to query.
    tail :
        An object string to query.
    ents_to_consider: array-like
        List of entities to use for triple completions. If None, will generate completions using all distinct entities.
        (Default: None.)
    rels_to_consider: array-like
        List of relations to use for triple completions. If None, will generate completions using all distinct
        relations. (Default: None.)

    Returns
    -------
    X : ndarray, shape [n, 3]
        A list of triples ordered by score.
    S : ndarray, shape [n]
       A list of scores.

    Examples
    --------

    >>> import requests
    >>> from ampligraph.datasets import load_from_csv
    >>> from ampligraph.latent_features import ComplEx
    >>> from ampligraph.discovery import discover_facts
    >>> from ampligraph.discovery import query_topn
    >>>
    >>> # Game of Thrones relations dataset
    >>> url = 'https://ampligraph.s3-eu-west-1.amazonaws.com/datasets/GoT.csv'
    >>> open('GoT.csv', 'wb').write(requests.get(url).content)
    >>> X = load_from_csv('.', 'GoT.csv', sep=',')
    >>>
    >>> model = ComplEx(batches_count=10, seed=0, epochs=200, k=150, eta=5,
    >>>                 optimizer='adam', optimizer_params={'lr':1e-3}, loss='multiclass_nll',
    >>>                 regularizer='LP', regularizer_params={'p':3, 'lambda':1e-5},
    >>>                 verbose=True)
    >>> model.fit(X)
    >>>
    >>> query_topn(model, top_n=5,
    >>>            head='Catelyn Stark', relation='ALLIED_WITH', tail=None,
    >>>            ents_to_consider=None, rels_to_consider=None)
    >>>
    (array([['Catelyn Stark', 'ALLIED_WITH', 'House Tully of Riverrun'],
            ['Catelyn Stark', 'ALLIED_WITH', 'House Stark of Winterfell'],
            ['Catelyn Stark', 'ALLIED_WITH', 'House Wayn'],
            ['Catelyn Stark', 'ALLIED_WITH', 'House Mollen'],
            ['Catelyn Stark', 'ALLIED_WITH', 'Orton Merryweather']],
           dtype='<U44'), array([[10.261374 ],
            [ 8.84298  ],
            [ 2.78139  ],
            [ 1.9809164],
            [ 1.833096 ]], dtype=float32))

    """

    if not model.is_fitted:
        msg = 'Model is not fitted.'
        logger.error(msg)
        raise ValueError(msg)

    if not np.sum([head is None, relation is None, tail is None]) == 1:
        msg = 'Exactly one of `head`, `relation` or `tail` arguments must be None.'
        logger.error(msg)
        raise ValueError(msg)

    if head:
        if head not in list(model.ent_to_idx.keys()):
            msg = 'Head entity `{}` not seen by model'.format(head)
            logger.error(msg)
            raise ValueError(msg)

    if relation:
        if relation not in list(model.rel_to_idx.keys()):
            msg = 'Relation `{}` not seen by model'.format(relation)
            logger.error(msg)
            raise ValueError(msg)

    if tail:
        if tail not in list(model.ent_to_idx.keys()):
            msg = 'Tail entity `{}` not seen by model'.format(tail)
            logger.error(msg)
            raise ValueError(msg)

    if ents_to_consider is not None:
        if head and tail:
            msg = 'Cannot specify `ents_to_consider` and both `subject` and `object` arguments.'
            logger.error(msg)
            raise ValueError(msg)
        if not isinstance(ents_to_consider, (list, np.ndarray)):
            msg = '`ents_to_consider` must be a list or numpy array.'
            logger.error(msg)
            raise ValueError(msg)
        if not all(x in list(model.ent_to_idx.keys()) for x in ents_to_consider):
            msg = 'Entities in `ents_to_consider` have not been seen by the model.'
            logger.error(msg)
            raise ValueError(msg)
        if len(ents_to_consider) < top_n:
            msg = '`ents_to_consider` contains less than top_n values, return set will be truncated.'
            logger.warning(msg)

    if rels_to_consider is not None:
        if relation:
            msg = 'Cannot specify both `rels_to_consider` and `relation` arguments.'
            logger.error(msg)
            raise ValueError(msg)
        if not isinstance(rels_to_consider, (list, np.ndarray)):
            msg = '`rels_to_consider` must be a list or numpy array.'
            logger.error(msg)
            raise ValueError(msg)
        if not all(x in list(model.rel_to_idx.keys()) for x in rels_to_consider):
            msg = 'Relations in `rels_to_consider` have not been seen by the model.'
            logger.error(msg)
            raise ValueError(msg)
        if len(rels_to_consider) < top_n:
            msg = '`rels_to_consider` contains less than top_n values, return set will be truncated.'
            logger.warning(msg)

    # Complete triples from entity and relation dict
    if relation is None:
        rels = rels_to_consider or list(model.rel_to_idx.keys())
        triples = np.array([[head, x, tail] for x in rels])
    else:
        ents = ents_to_consider or list(model.ent_to_idx.keys())
        if head:
            triples = np.array([[head, relation, x] for x in ents])
        else:
            triples = np.array([[x, relation, tail] for x in ents])

    # Get scores for completed triples
    scores = model.predict(triples)

    # Join triples and scores, sort ascending by scores, then take top_n results
    topn_idx = np.squeeze(np.argsort(scores, axis=0)[::-1][:top_n])
    scores_out = np.array(scores)[topn_idx]
    triples_out = np.copy(triples[topn_idx, :])

    return triples_out, scores_out
